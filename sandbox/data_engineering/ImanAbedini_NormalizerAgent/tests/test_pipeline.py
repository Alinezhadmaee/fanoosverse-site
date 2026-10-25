"""End-to-end pipeline tests: intake, deterministic rules, LLM stage, cache,
validation, envelope assembly."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from normalizer_agent.cache.sqlite_cache import LLMCache
from normalizer_agent.interface.api import Normalizer
from normalizer_agent.llm.base import LLMRequest, LLMResponse
from normalizer_agent.llm.providers.mock import MockProvider
from normalizer_agent.llm.router import FallbackRouter, _Entry
from normalizer_agent.pipeline.orchestrator import RecordNormalizer
from normalizer_agent.review.sink import MemoryReviewSink
from normalizer_agent.schemas.envelope import RecordStatus
from normalizer_agent.schemas.job_ad import JobAd
from normalizer_agent.settings import LLMConfig, ProviderConfig, RoutingConfig, load_app_config
from normalizer_agent.vocab.loader import VocabStore
from normalizer_agent.vocab.retriever import TrigramRetriever


def _create_router_with_provider(
    provider, app_config, name="mock_test", priority=1
) -> FallbackRouter:
    p_cfg = ProviderConfig(name=name, type="mock", model="mock-model", priority=priority)
    llm_cfg = LLMConfig(
        routing=RoutingConfig(strategy="priority_fallback", max_total_attempts=4),
        providers=[p_cfg],
    )
    router = FallbackRouter.__new__(FallbackRouter)
    router._llm_config = llm_cfg
    router._pipeline = app_config.pipeline
    entry = _Entry(p_cfg, app_config.pipeline, provider=provider)
    router._entries = [entry]
    router._rr_index = 0
    router._call_lock = asyncio.Lock()
    return router


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_raw(project_root: Path) -> dict[str, Any]:
    path = project_root / "tests" / "data" / "sample_record_1.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def app_config(project_root: Path):
    return load_app_config(project_root / "config")


@pytest.fixture
def vocab_store(project_root: Path):
    return VocabStore(project_root / "config" / "vocab")


@pytest.fixture
def tmp_cache(tmp_path: Path):
    cache = LLMCache(tmp_path / "test_cache.sqlite", enabled=True)
    yield cache
    cache.close()


@pytest.fixture
def mock_router(app_config, project_root: Path):
    p_cfg = ProviderConfig(
        name="mock_test",
        type="mock",
        model="mock-model",
        priority=1,
    )
    provider = MockProvider(p_cfg)
    return _create_router_with_provider(provider, app_config)


@pytest.mark.asyncio
async def test_end_to_end_pipeline_with_mock(
    app_config, vocab_store, mock_router, tmp_cache, sample_raw
):
    sink = MemoryReviewSink()
    normalizer = RecordNormalizer(
        config=app_config,
        vocab_store=vocab_store,
        router=mock_router,
        cache=tmp_cache,
        review_sink=sink,
        retriever=TrigramRetriever(),
    )

    result = await normalizer.normalize(sample_raw)

    assert result.status in (RecordStatus.NORMALIZED, RecordStatus.NEEDS_REVIEW)
    assert result.record is not None
    assert isinstance(result.record, JobAd)

    # Provenance fields unchanged
    assert result.record.document_id == sample_raw["document_id"]
    assert result.record.text == sample_raw["text"]
    assert result.record.content_hash == sample_raw["content_hash"]
    assert result.record.collector == sample_raw["collector"]

    # Canonical mapping checks
    assert "Python" in result.record.skills_required
    assert "PyTorch" in result.record.skills_required
    assert "Docker" in result.record.tools
    assert "Kubernetes" in result.record.tools

    # Salary parsing
    assert result.record.salary_min == 380_000_000
    assert result.record.salary_max == 520_000_000
    assert result.record.salary_currency == "IRR"

    # Audit entries and reviews recorded
    assert len(result.audit) > 0
    assert result.versions["normalizer"] is not None
    assert "capability" in result.versions["vocab"]


@pytest.mark.asyncio
async def test_cache_hits_skip_llm(app_config, vocab_store, mock_router, tmp_cache, sample_raw):
    normalizer = RecordNormalizer(
        config=app_config,
        vocab_store=vocab_store,
        router=mock_router,
        cache=tmp_cache,
        review_sink=None,
        retriever=TrigramRetriever(),
    )

    # First run fills the cache
    res1 = await normalizer.normalize(sample_raw)

    # Second run should read from cache
    res2 = await normalizer.normalize(sample_raw)

    assert res2.usage.llm_calls == 0
    assert res2.usage.input_tokens == 0
    # Canonical results match
    assert res1.record.skills_required == res2.record.skills_required
    assert res1.record.salary_min == res2.record.salary_min


@pytest.mark.asyncio
async def test_intake_coercion_rejects_missing_provenance(
    app_config, vocab_store, mock_router, tmp_cache
):
    normalizer = RecordNormalizer(
        config=app_config,
        vocab_store=vocab_store,
        router=mock_router,
        cache=tmp_cache,
    )
    bad_record = {
        "text": "Some text without document_id",
        # missing document_id and content_hash
    }

    result = await normalizer.normalize(bad_record)
    assert result.status == RecordStatus.REJECTED
    assert any(code in ("MISSING_REQUIRED_FIELD", "SCHEMA_INVALID") for code in result.error_codes)
    assert result.record is None


@pytest.mark.asyncio
async def test_repair_loop_on_malformed_llm_json(app_config, vocab_store, tmp_cache):
    class FlakyJsonProvider:
        def __init__(self):
            self.name = "flaky"
            self.model = "flaky-v1"
            self.cost_per_1k_input = 0.001
            self.cost_per_1k_output = 0.002
            self.attempt = 0

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.attempt += 1
            if self.attempt == 1:
                # First attempt: invalid shape (missing items key)
                bad_json = json.dumps({"wrong_key": []})
                return LLMResponse(
                    text=bad_json,
                    input_tokens=50,
                    output_tokens=10,
                    latency_ms=10.0,
                    cost_usd=0.0001,
                    provider=self.name,
                    model=self.model,
                )
            # Second attempt (repair): correct shape
            # Parse requested items from user prompt
            payload = json.loads(request.user.split("\n\n### VALIDATION ERROR")[0])
            items_ans = []
            for item in payload.get("items", []):
                items_ans.append(
                    {
                        "field": item["field"],
                        "index": item["index"],
                        "canonical": [item["raw_value"]],
                        "category": None,
                        "confidence": "high",
                        "reason": "repaired",
                    }
                )
            good_json = json.dumps({"items": items_ans})
            return LLMResponse(
                text=good_json,
                input_tokens=100,
                output_tokens=30,
                latency_ms=15.0,
                cost_usd=0.0002,
                provider=self.name,
                model=self.model,
            )

        async def aclose(self):
            pass

    flaky = FlakyJsonProvider()
    router = _create_router_with_provider(flaky, app_config, name="flaky")

    normalizer = RecordNormalizer(
        config=app_config,
        vocab_store=vocab_store,
        router=router,
        cache=tmp_cache,
    )

    raw = {
        "document_id": "repair_test_01",
        "collector": "test",
        "source_type": "job_posting",
        "text": "sample text",
        "content_hash": "hash1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "collected_at": "2026-09-01",
        "skills_required": ["CompletelyUnknownSkillXYZ"],
    }

    result = await normalizer.normalize(raw)
    assert flaky.attempt == 2  # Proves repair request was triggered
    assert result.status in (RecordStatus.NORMALIZED, RecordStatus.NEEDS_REVIEW)


@pytest.mark.asyncio
async def test_top_level_api_facade(project_root: Path, sample_raw):
    normalizer = await Normalizer.from_config(project_root / "config")
    p_cfg = ProviderConfig(name="mock_test", type="mock", model="mock-model", priority=1)
    provider = MockProvider(p_cfg)
    mock_router = _create_router_with_provider(provider, normalizer.config)
    normalizer.router = mock_router
    normalizer._normalizer.router = mock_router
    normalizer._normalizer.llm_stage.router = mock_router
    try:
        result = await normalizer.normalize(sample_raw)
        assert result.status in (RecordStatus.NORMALIZED, RecordStatus.NEEDS_REVIEW)
        assert normalizer.metrics.records_in >= 1

        batch = await normalizer.normalize_batch([sample_raw, sample_raw])
        assert len(batch) == 2
        assert normalizer.metrics.records_in >= 3
    finally:
        await normalizer.aclose()
