"""Public Python API.

from normalizer_agent.interface.api import Normalizer

normalizer = await Normalizer.from_config("config")
result = await normalizer.normalize(record)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from normalizer_agent.cache.sqlite_cache import LLMCache
from normalizer_agent.llm.router import FallbackRouter, build_router
from normalizer_agent.observability.logging import Metrics, setup_logging
from normalizer_agent.pipeline.orchestrator import RecordNormalizer
from normalizer_agent.review.sink import NullReviewSink, ReviewSink
from normalizer_agent.schemas.envelope import NormalizeResult, RecordStatus
from normalizer_agent.schemas.job_ad import JobAd
from normalizer_agent.settings import AppConfig, load_app_config
from normalizer_agent.startup_check import check_fields_config
from normalizer_agent.vocab.loader import VocabStore
from normalizer_agent.vocab.retriever import TrigramRetriever

logger = logging.getLogger(__name__)


class Normalizer:
    """Facade over the whole pipeline; one instance per process."""

    def __init__(
        self,
        config: AppConfig,
        vocab_store: VocabStore,
        router: FallbackRouter,
        cache: LLMCache,
        review_sink: ReviewSink | None = None,
    ) -> None:
        self.config = config
        self.vocab_store = vocab_store
        self.router = router
        self.cache = cache
        self.metrics = Metrics()
        self._normalizer = RecordNormalizer(
            config=config,
            vocab_store=vocab_store,
            router=router,
            cache=cache,
            review_sink=review_sink or NullReviewSink(),
            retriever=TrigramRetriever(),
        )
        self._semaphore = asyncio.Semaphore(config.pipeline.concurrency.max_concurrency)

    @classmethod
    async def from_config(
        cls,
        config_dir: str | Path = "config",
        *,
        review_sink: ReviewSink | None = None,
        cache_path: str | Path | None = None,
    ) -> Normalizer:
        setup_logging()
        config_dir = Path(config_dir)
        config = load_app_config(config_dir)
        check_fields_config(config.fields, JobAd)
        vocab_store = VocabStore(config_dir / "vocab")
        router = build_router(config.llm, config.pipeline)
        cache = LLMCache(
            cache_path or (config_dir / config.pipeline.cache.path),
            enabled=config.pipeline.cache.enabled,
        )
        return cls(config, vocab_store, router, cache, review_sink)

    # ------------------------------------------------------------------ #

    async def normalize(
        self,
        record: dict[str, Any],
        *,
        record_id: str | None = None,
        trace_id: str | None = None,
    ) -> NormalizeResult:
        self.metrics.records_in += 1
        async with self._semaphore:
            try:
                result = await self._normalizer.normalize(
                    record, record_id=record_id, trace_id=trace_id
                )
            except Exception:  # noqa: BLE001 - isolate record failures
                logger.exception("record crashed the pipeline")
                result = NormalizeResult(
                    record_id=record_id or str(record.get("document_id", "<unknown>")),
                    status=RecordStatus.FAILED,
                    error_codes=["INTERNAL_ERROR"],
                    trace_id=trace_id,
                )
                result.__dict__.setdefault("audit", [])
        self._account(result)
        return result

    async def normalize_batch(
        self,
        records: Iterable[dict[str, Any] | tuple[str, dict[str, Any]]],
        *,
        trace_ids: Sequence[str | None] | None = None,
    ) -> list[NormalizeResult]:
        tasks = []
        for position, entry in enumerate(records):
            if isinstance(entry, tuple):
                record_id, record = entry
            else:
                record_id, record = None, entry
            trace_id = trace_ids[position] if trace_ids else None
            tasks.append(self.normalize(record, record_id=record_id, trace_id=trace_id))
        return list(await asyncio.gather(*tasks))

    async def aclose(self) -> None:
        await self.router.aclose()
        self.cache.close()

    async def __aenter__(self) -> Normalizer:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _account(self, result: NormalizeResult) -> None:
        status = result.status
        if status is RecordStatus.NORMALIZED:
            self.metrics.records_normalized += 1
        elif status is RecordStatus.NEEDS_REVIEW:
            self.metrics.records_needs_review += 1
        elif status is RecordStatus.FAILED:
            self.metrics.records_failed += 1
        else:
            self.metrics.records_rejected += 1
        self.metrics.items_review += len(result.review_items)
        self.metrics.llm_input_tokens += result.usage.input_tokens
        self.metrics.llm_output_tokens += result.usage.output_tokens
        self.metrics.llm_cost_usd += result.usage.estimated_cost_usd
        self.metrics.llm_calls += result.usage.llm_calls
        for entry in result.audit:
            key = {
                "rule": "items_rule",
                "lookup": "items_lookup",
                "llm": "items_llm",
                "cache": "items_cache",
            }.get(entry.method)
            if key:
                setattr(self.metrics, key, getattr(self.metrics, key) + 1)
