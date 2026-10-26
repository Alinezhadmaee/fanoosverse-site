"""FallbackRouter: priority fallback, circuit breakers, rate limiting.

Implements the same :class:`LLMProvider` interface as a single provider, so
the pipeline never knows about fallback. Behavior (task 5.2):

- providers are tried by ``priority`` (round_robin rotates the start);
- transient errors (429/5xx/timeouts) are retried inside the adapter with
  backoff + ``Retry-After``; the router moves to the next provider after that;
- permanent errors (401/403/404/400) trip the circuit for a long cooldown and
  the router moves on immediately;
- output-quality failures (unparseable JSON) are NOT outages: one repair
  retry on the same provider, then escalate; if every provider produced only
  unusable output the router raises :class:`OutputQualityFailureError`
  (record -> needs_review), otherwise :class:`AllProvidersFailedError`
  (record -> failed, safe to retry).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

from normalizer_agent.llm.base import (
    AllProvidersFailedError,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    OutputQualityError,
    OutputQualityFailureError,
    ProviderPermanentError,
    ProviderTransientError,
    parse_json_tolerant,
)
from normalizer_agent.llm.registry import build_provider
from normalizer_agent.llm.resilience import CircuitBreaker, RateLimiter
from normalizer_agent.settings import LLMConfig, PipelineConfig, ProviderConfig

logger = logging.getLogger(__name__)


class _Entry:
    __slots__ = ("config", "provider", "breaker", "limiter")

    def __init__(
        self,
        config: ProviderConfig,
        pipeline: PipelineConfig,
        provider: LLMProvider | None = None,
    ) -> None:
        self.config = config
        self.provider: LLMProvider = provider if provider is not None else build_provider(config)
        cb = pipeline.circuit_breaker
        self.breaker = CircuitBreaker(
            failure_threshold=cb.failure_threshold,
            cooldown_s=cb.cooldown_s,
            permanent_cooldown_s=cb.permanent_cooldown_s,
        )
        self.limiter = (
            RateLimiter(config.requests_per_minute) if config.requests_per_minute else None
        )


class FallbackRouter(LLMProvider):
    name = "router"
    model = "*"

    def __init__(self, llm_config: LLMConfig, pipeline_config: PipelineConfig) -> None:
        self._llm_config = llm_config
        self._pipeline = pipeline_config
        self._entries: list[_Entry] = []
        for provider_config in llm_config.enabled_providers():
            try:
                self._entries.append(_Entry(provider_config, pipeline_config))
            except LLMError as exc:
                # e.g. openai_compat without base_url; logged, provider skipped
                logger.warning("skipping provider: %s", exc)
        if not self._entries:
            raise LLMError("no usable provider: all providers failed to initialize")
        self._rr_index = 0
        self._call_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #

    async def complete(self, request: LLMRequest) -> LLMResponse:
        reasons: dict[str, str] = {}
        quality_reasons: dict[str, str] = {}
        attempts_left = max(1, self._llm_config.routing.max_total_attempts)

        for entry in self._ordered_entries():
            if attempts_left <= 0:
                break
            attempts_left -= 1
            name = entry.config.name
            if not entry.breaker.allow():
                reasons[name] = f"circuit open ({entry.breaker.describe()})"
                logger.warning("provider '%s' skipped: circuit open", name)
                continue
            if entry.limiter is not None:
                await entry.limiter.acquire()
            try:
                response = await self._complete_on(entry, request)
            except OutputQualityError as exc:
                quality_reasons[name] = str(exc)
                logger.warning("provider '%s': unusable output: %s", name, exc)
                continue
            except ProviderPermanentError as exc:
                entry.breaker.record_failure(permanent=True)
                reasons[name] = str(exc)
                logger.error("provider '%s' permanent failure, moving on: %s", name, exc)
                continue
            except ProviderTransientError as exc:
                entry.breaker.record_failure()
                reasons[name] = str(exc)
                logger.warning("provider '%s' transient failure, moving on: %s", name, exc)
                if exc.retry_after_s:
                    await asyncio.sleep(min(exc.retry_after_s, 5.0))
                continue
            entry.breaker.record_success()
            return response

        if quality_reasons and not reasons:
            raise OutputQualityFailureError(quality_reasons)
        reasons.update(quality_reasons)
        raise AllProvidersFailedError(reasons)

    async def _complete_on(self, entry: _Entry, request: LLMRequest) -> LLMResponse:
        """One provider: attempt + one JSON-repair retry on quality failure."""

        try:
            response = await entry.provider.complete(request)
            if request.expect_json:
                parse_json_tolerant(response.text)  # raises OutputQualityError
            return response
        except OutputQualityError as first_error:
            repair = LLMRequest(
                system=request.system,
                user=(
                    request.user
                    + "\n\nIMPORTANT: your previous answer could not be parsed as JSON "
                    f"({first_error}). Answer again with STRICT JSON only, no prose, "
                    "no markdown fences."
                ),
                max_tokens=request.max_tokens,
                expect_json=request.expect_json,
                metadata=request.metadata,
                repair_note="invalid_json",
            )
            response = await entry.provider.complete(repair)
            if request.expect_json:
                parse_json_tolerant(response.text)
            return response

    def _ordered_entries(self) -> list[_Entry]:
        ordered = list(self._entries)
        if self._llm_config.routing.strategy == "round_robin":
            ordered = ordered[self._rr_index :] + ordered[: self._rr_index]
            self._rr_index = (self._rr_index + 1) % max(1, len(ordered))
        return ordered

    # ------------------------------------------------------------------ #

    async def ping(self) -> float:
        """Ping the first reachable provider; returns its latency in ms."""

        reasons: dict[str, str] = {}
        for entry in self._entries:
            if not entry.breaker.allow():
                reasons[entry.config.name] = "circuit open"
                continue
            try:
                latency = await entry.provider.ping()
            except LLMError as exc:
                reasons[entry.config.name] = str(exc)
                continue
            return latency
        raise AllProvidersFailedError(reasons)

    async def ping_all(self) -> dict[str, dict[str, Any]]:
        """Health table for `normalizer llm-check`."""

        results: dict[str, dict[str, Any]] = {}

        async def one(entry: _Entry) -> None:
            record: dict[str, Any] = {
                "model": entry.config.model,
                "type": entry.config.type,
                "priority": entry.config.priority,
                "circuit": entry.breaker.describe(),
            }
            if not entry.breaker.allow():
                record["status"] = "skipped(circuit open)"
                results[entry.config.name] = record
                return
            started = time.perf_counter()
            try:
                await entry.provider.ping()
                record["status"] = "ok"
                record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            except LLMError as exc:
                record["status"] = "error"
                record["error"] = str(exc)[:200]
            results[entry.config.name] = record

        await asyncio.gather(*(one(entry) for entry in self._entries))
        return results

    def snapshot(self) -> dict[str, str]:
        return {e.config.name: e.breaker.describe() for e in self._entries}

    async def aclose(self) -> None:
        for entry in self._entries:
            await entry.provider.aclose()

    # convenience for observability tests -------------------------------- #

    def provider_names(self) -> list[str]:
        return [e.config.name for e in self._entries]

    @staticmethod
    def dump_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)


def build_router(llm_config: LLMConfig, pipeline_config: PipelineConfig) -> FallbackRouter:
    backoff = pipeline_config.retries
    random.seed()  # jitter source
    logger.debug(
        "router built: backoff base=%.2fs max=%.2fs", backoff.backoff_base_s, backoff.backoff_max_s
    )
    return FallbackRouter(llm_config, pipeline_config)
