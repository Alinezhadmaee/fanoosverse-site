"""Adapter tests over httpx.MockTransport (no real network) + config-swap test."""

from __future__ import annotations

import json

import httpx
import pytest

from normalizer_agent.llm.base import (
    LLMRequest,
    ProviderPermanentError,
    ProviderTransientError,
)
from normalizer_agent.llm.providers.anthropic import AnthropicProvider
from normalizer_agent.llm.providers.openai_compat import OpenAICompatProvider
from normalizer_agent.llm.router import FallbackRouter
from normalizer_agent.settings import (
    LLMConfig,
    PipelineConfig,
    ProviderConfig,
    RoutingConfig,
)


def _anthropic_with_handler(handler) -> AnthropicProvider:
    config = ProviderConfig(
        name="anthropic-test",
        type="anthropic",
        model="test-model",
        api_key_env="TEST_KEY_FAKE",
        max_retries=1,
        timeout_s=5,
        params={},
        cost_per_mtok={"input": 0.1, "output": 0.2},
    )
    provider = AnthropicProvider(config)
    provider._client = httpx.AsyncClient(
        base_url="https://mock.local",
        transport=httpx.MockTransport(handler),
        headers={"x-api-key": "k", "anthropic-version": "x"},
    )
    return provider


def _openai_with_handler(handler) -> OpenAICompatProvider:
    config = ProviderConfig(
        name="openai-test",
        type="openai_compat",
        model="test-model",
        base_url="https://mock.local/v1",
        api_key_env="TEST_KEY_FAKE",
        params={"temperature": 0},
        features={"json_mode": True},
    )
    provider = OpenAICompatProvider(config)
    provider._client = httpx.AsyncClient(
        base_url="https://mock.local/v1",
        transport=httpx.MockTransport(handler),
        headers={"authorization": "Bearer k", "content-type": "application/json"},
    )
    return provider


class TestAnthropicAdapter:
    async def test_success_and_cost(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["model"] == "test-model"
            assert payload["system"] == "sys"
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": '{"ok": 1}'}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "stop_reason": "end_turn",
                },
            )

        provider = _anthropic_with_handler(handler)
        response = await provider.complete(LLMRequest(system="sys", user="hi"))
        assert response.text == '{"ok": 1}'
        assert response.provider == "anthropic-test"
        assert response.input_tokens == 100
        assert abs(response.cost_usd - (100 * 0.1 + 50 * 0.2) / 1_000_000) < 1e-12

    async def test_429_with_retry_after_header(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "0.01"}, text="rate")
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}}
            )

        provider = _anthropic_with_handler(handler)
        response = await provider.complete(LLMRequest(system="", user="hi"))
        assert response.text == "ok" and calls["n"] == 2

    async def test_401_is_permanent(self) -> None:
        provider = _anthropic_with_handler(lambda request: httpx.Response(401, text="invalid key"))
        with pytest.raises(ProviderPermanentError):
            await provider.complete(LLMRequest(system="", user="hi"))

    async def test_timeout_is_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom")

        provider = _anthropic_with_handler(handler)
        with pytest.raises(ProviderTransientError):
            await provider.complete(LLMRequest(system="", user="hi"))


class TestOpenAICompatAdapter:
    async def test_payload_shape_and_json_mode(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_KEY_FAKE", "k")
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer k"
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"x": 2}'}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    "model": "test-model",
                },
            )

        provider = _openai_with_handler(handler)
        response = await provider.complete(LLMRequest(system="s", user="u"))
        assert seen["model"] == "test-model"
        assert seen["response_format"] == {"type": "json_object"}
        assert seen["temperature"] == 0  # params passed through
        assert response.text == '{"x": 2}'

    async def test_5xx_then_success(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, text="down")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
            )

        provider = _openai_with_handler(handler)
        response = await provider.complete(LLMRequest(system="", user="u"))
        assert response.text == "ok" and calls["n"] == 2


class TestConfigSwap:
    """Changing YAML (model/order/enabled) must change behavior with zero code change."""

    def _cfg(self, **over) -> ProviderConfig:
        base = dict(name="mock", type="mock", model="m1", priority=1, params={})
        base.update(over)
        return ProviderConfig(**base)

    def _router(self, providers: list[ProviderConfig]) -> FallbackRouter:
        return FallbackRouter(
            LLMConfig(routing=RoutingConfig(max_total_attempts=6), providers=providers),
            PipelineConfig(),
        )

    EMPTY_PAYLOAD = '{"items": []}'

    async def test_model_change_visible_in_response(self) -> None:
        router = self._router([self._cfg(model="model-A")])
        response = await router.complete(
            LLMRequest(system="s", user=self.EMPTY_PAYLOAD, expect_json=False)
        )
        assert response.model == "model-A"

        router2 = self._router([self._cfg(model="model-B")])
        response2 = await router2.complete(
            LLMRequest(system="s", user=self.EMPTY_PAYLOAD, expect_json=False)
        )
        assert response2.model == "model-B"

    async def test_disabling_provider_changes_routing(self) -> None:
        # primary disabled -> backup serves
        router = self._router(
            [
                self._cfg(name="primary", model="m1", priority=1, enabled=False),
                self._cfg(name="backup", model="m2", priority=2),
            ]
        )
        assert router.provider_names() == ["backup"]
        response = await router.complete(
            LLMRequest(system="s", user=self.EMPTY_PAYLOAD, expect_json=False)
        )
        assert response.provider == "backup"

    async def test_reordering_changes_priority(self) -> None:
        router = self._router(
            [
                self._cfg(name="second", model="m1", priority=2),
                self._cfg(name="first", model="m1", priority=1),
            ]
        )
        response = await router.complete(
            LLMRequest(system="s", user=self.EMPTY_PAYLOAD, expect_json=False)
        )
        assert response.provider == "first"
