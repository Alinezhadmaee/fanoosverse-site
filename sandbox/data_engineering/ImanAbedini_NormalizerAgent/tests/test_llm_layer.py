"""Provider-layer tests with fake providers (no network, task 9.5)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

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
from normalizer_agent.llm.providers.mock import MockProvider
from normalizer_agent.llm.registry import build_provider, known_types
from normalizer_agent.llm.resilience import CircuitBreaker, CircuitState
from normalizer_agent.llm.router import FallbackRouter
from normalizer_agent.settings import (
    CircuitConfig,
    LLMConfig,
    PipelineConfig,
    ProviderConfig,
    RoutingConfig,
)


def _req(**kwargs: Any) -> LLMRequest:
    defaults = {"system": "s", "user": '{"items": []}', "expect_json": False}
    defaults.update(kwargs)
    return LLMRequest(**defaults)


class FakeProvider(LLMProvider):
    """Scriptable fake; ``script`` is a list of LLMResponse|Exception per call."""

    name = "fake"
    model = "fake-model"

    def __init__(self, script: list[Any], name: str = "fake", model: str = "fake-model") -> None:
        self.script = list(script)
        self.calls = 0
        self.name = name
        self.model = model
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.requests.append(request)
        outcome = self.script.pop(0) if self.script else ProviderTransientError("exhausted")
        if isinstance(outcome, Exception):
            raise outcome
        outcome.provider = self.name
        outcome.model = self.model
        return outcome

    async def ping(self) -> float:
        self.calls += 1
        if self.script and isinstance(self.script[0], Exception):
            raise self.script.pop(0)
        return 1.0


def _resp(text: str, tokens: int = 10) -> LLMResponse:
    return LLMResponse(
        text=text,
        input_tokens=100,
        output_tokens=tokens,
        latency_ms=12.0,
        provider="?",
        model="?",
        finish_reason="stop",
    )


def _router(
    entries: list[tuple[ProviderConfig, LLMProvider]],
    *,
    strategy: str = "priority_fallback",
    max_attempts: int = 6,
    circuit: CircuitConfig | None = None,
) -> FallbackRouter:
    llm_cfg = LLMConfig(
        routing=RoutingConfig(strategy=strategy, max_total_attempts=max_attempts),
        providers=[config for config, _ in entries],
    )
    pipeline_cfg = PipelineConfig(circuit_breaker=circuit or CircuitConfig())
    router = FallbackRouter.__new__(FallbackRouter)
    from normalizer_agent.llm.router import _Entry

    router._llm_config = llm_cfg
    router._pipeline = pipeline_cfg
    router._entries = []
    for config, provider in entries:
        entry = _Entry(config, pipeline_cfg, provider=provider)
        router._entries.append(entry)
    router._rr_index = 0
    router._call_lock = asyncio.Lock()
    return router


def _cfg(name: str, priority: int) -> ProviderConfig:
    return ProviderConfig(name=name, type="fake", model="m", priority=priority)


class TestRouter:
    async def test_primary_5xx_backup_used(self) -> None:
        primary = FakeProvider([ProviderTransientError("500")], name="primary")
        backup = FakeProvider([_resp('{"ok": true}')], name="backup")
        router = _router([(_cfg("primary", 1), primary), (_cfg("backup", 2), backup)])
        response = await router.complete(_req())
        assert response.provider == "backup"
        assert primary.calls == 1 and backup.calls == 1

    async def test_401_skipped_without_retries(self) -> None:
        primary = FakeProvider([ProviderPermanentError("401")], name="primary")
        backup = FakeProvider([_resp("ok")], name="backup")
        router = _router([(_cfg("primary", 1), primary), (_cfg("backup", 2), backup)])
        await router.complete(_req())
        assert primary.calls == 1  # no retry on permanent error
        assert backup.calls == 1

    async def test_all_down_raises_all_providers_failed(self) -> None:
        a = FakeProvider([ProviderTransientError("down")], name="a")
        b = FakeProvider([ProviderPermanentError("401")], name="b")
        router = _router([(_cfg("a", 1), a), (_cfg("b", 2), b)])
        with pytest.raises(AllProvidersFailedError) as excinfo:
            await router.complete(_req())
        assert set(excinfo.value.reasons) == {"a", "b"}

    async def test_max_total_attempts_caps_visits(self) -> None:
        p1 = FakeProvider([ProviderTransientError("x")], name="p1")
        p2 = FakeProvider([ProviderTransientError("x")], name="p2")
        p3 = FakeProvider([_resp("ok")], name="p3")
        router = _router(
            [(_cfg("p1", 1), p1), (_cfg("p2", 2), p2), (_cfg("p3", 3), p3)],
            max_attempts=2,
        )
        with pytest.raises(AllProvidersFailedError):
            await router.complete(_req())
        assert p3.calls == 0

    async def test_invalid_json_repair_retry_then_next_provider(self) -> None:
        primary = FakeProvider([_resp("not json at all"), _resp('{"good": 1}')], name="primary")
        router = _router([(_cfg("primary", 1), primary)])
        request = _req(expect_json=True)
        response = await router.complete(request)
        assert primary.calls == 2
        assert response.provider == "primary"
        assert "STRICT JSON" in primary.requests[1].user

    async def test_invalid_json_everywhere_is_quality_failure_not_outage(self) -> None:
        primary = FakeProvider([_resp("garbage"), _resp("still garbage")], name="primary")
        router = _router([(_cfg("primary", 1), primary)])
        with pytest.raises(OutputQualityFailureError):
            await router.complete(_req(expect_json=True))
        assert primary.calls == 2

    async def test_mixed_quality_and_outage_is_all_providers_failed(self) -> None:
        primary = FakeProvider([_resp("garbage"), _resp("garbage")], name="primary")
        backup = FakeProvider([ProviderPermanentError("401")], name="backup")
        router = _router([(_cfg("primary", 1), primary), (_cfg("backup", 2), backup)])
        with pytest.raises(AllProvidersFailedError):
            await router.complete(_req(expect_json=True))

    async def test_circuit_breaker_opens_and_half_opens(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, cooldown_s=0.05)
        primary = FakeProvider(
            [
                ProviderTransientError("f1"),
                ProviderTransientError("f2"),
                _resp("ok"),
            ],
            name="primary",
        )
        router = _router(
            [(_cfg("primary", 1), primary)],
            circuit=CircuitConfig(failure_threshold=2, cooldown_s=0.05),
        )
        router._entries[0].breaker = breaker
        with pytest.raises(AllProvidersFailedError):
            await router.complete(_req())  # failure 1
        with pytest.raises(AllProvidersFailedError):
            await router.complete(_req())  # failure 2 -> opens
        assert breaker.state is CircuitState.OPEN
        with pytest.raises(AllProvidersFailedError):
            await router.complete(_req())  # skipped, circuit open
        assert primary.calls == 2
        await asyncio.sleep(0.06)
        response = await router.complete(_req())  # half-open probe succeeds
        assert response.provider == "primary"
        assert breaker.state is CircuitState.CLOSED

    async def test_success_keeps_circuit_closed(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2)
        primary = FakeProvider([_resp("ok"), _resp("ok")], name="primary")
        router = _router([(_cfg("primary", 1), primary)])
        router._entries[0].breaker = breaker
        await router.complete(_req())
        await router.complete(_req())
        assert breaker.consecutive_failures == 0

    async def test_round_robin_rotates_start(self) -> None:
        a = FakeProvider([_resp("A"), _resp("A"), _resp("A")], name="a")
        b = FakeProvider([_resp("B"), _resp("B"), _resp("B")], name="b")
        router = _router([(_cfg("a", 1), a), (_cfg("b", 2), b)], strategy="round_robin")
        assert (await router.complete(_req())).provider == "a"
        assert (await router.complete(_req())).provider == "b"
        assert (await router.complete(_req())).provider == "a"


class TestCircuitBreaker:
    def test_opens_after_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow()
        breaker.record_failure()
        assert not breaker.allow()
        assert breaker.state is CircuitState.OPEN

    def test_permanent_trips_immediately(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure(permanent=True)
        assert not breaker.allow()


class TestParseJson:
    def test_plain(self) -> None:
        assert parse_json_tolerant('{"a": 1}') == {"a": 1}

    def test_fenced(self) -> None:
        assert parse_json_tolerant('```json\n{"a": 1}\n```') == {"a": 1}

    def test_surrounded_by_prose(self) -> None:
        assert parse_json_tolerant('Here you go: {"a": 1} thanks') == {"a": 1}

    def test_failure(self) -> None:
        with pytest.raises(OutputQualityError):
            parse_json_tolerant("hello world")


class TestRegistryAndMock:
    def test_known_types_include_builtin(self) -> None:
        assert {"anthropic", "openai_compat", "mock"} <= set(known_types())

    def test_unknown_type_clear_error(self) -> None:
        with pytest.raises(LLMError, match="unknown provider type"):
            build_provider(ProviderConfig(name="x", type="nope", model="m"))

    def test_mock_scripted_then_exhausted(self) -> None:
        provider = build_provider(
            ProviderConfig(
                name="mock",
                type="mock",
                model="mock-1",
                params={"scripted": ['{"a": 1}']},
            )
        )
        assert isinstance(provider, MockProvider)

        async def run() -> str:
            first = await provider.complete(_req(expect_json=True))
            return first.text

        text = asyncio.run(run())
        assert text == '{"a": 1}'

    def test_mock_missing_api_key_ok(self) -> None:
        provider = build_provider(ProviderConfig(name="mock", type="mock", model="m"))
        assert provider.model == "m"
