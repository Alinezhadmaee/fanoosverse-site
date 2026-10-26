"""Anthropic Messages API adapter.

All vendor details (endpoint, headers, payload shape, token accounting) live
here and nowhere else.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from normalizer_agent.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderPermanentError,
    ProviderTransientError,
)
from normalizer_agent.llm.registry import register, resolve_api_key
from normalizer_agent.settings import ProviderConfig

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


@register("anthropic")
def factory(config: ProviderConfig) -> LLMProvider:
    return AnthropicProvider(config)


class AnthropicProvider(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.name = config.name
        self.model = config.model
        self._config = config
        self._api_key = resolve_api_key(config)
        self._base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self._api_key:
                raise ProviderPermanentError(
                    f"provider '{self.name}': api key env var "
                    f"{self._config.api_key_env} is not set",
                    http_status=401,
                )
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._config.timeout_s,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
            )
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._ensure_client()
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": request.user}],
        }
        if request.system:
            payload["system"] = request.system
        # params pass through as-is (top-level Messages API fields only)
        for key, value in self._config.params.items():
            payload.setdefault(key, value)
        if self._config.features.prompt_caching:
            payload["messages"] = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": request.user,
                            # stable prompt prefix benefits from caching
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ]

        started = time.perf_counter()
        response = await self._request_with_retries(client, "/v1/messages", payload)
        latency_ms = (time.perf_counter() - started) * 1000
        data = response.json()
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            provider=self.name,
            model=data.get("model", self.model),
            finish_reason=data.get("stop_reason"),
            cost_usd=self._config.cost_usd(input_tokens, output_tokens),
        )

    async def _request_with_retries(
        self, client: httpx.AsyncClient, path: str, payload: dict[str, Any]
    ) -> httpx.Response:
        attempts = max(1, self._config.max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await client.post(path, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = ProviderTransientError(
                    f"provider '{self.name}': transport failure: {exc!r}"
                )
            else:
                if response.status_code < 400:
                    return response
                last_error = self._http_error(response)
                if isinstance(last_error, ProviderPermanentError):
                    raise last_error
            if attempt < attempts - 1:
                retry_after = getattr(last_error, "retry_after_s", None)
                import asyncio
                import random

                delay = (
                    retry_after
                    if retry_after is not None
                    else 0.5 * (2**attempt) + random.uniform(0, 0.1)
                )
                await asyncio.sleep(min(delay, 5.0))
        assert last_error is not None
        raise last_error

    def _http_error(self, response: httpx.Response) -> Exception:
        status = response.status_code
        body = response.text[:300]
        if status == 429:
            retry_after = response.headers.get("retry-after")
            seconds = (
                float(retry_after)
                if retry_after and retry_after.replace(".", "", 1).isdigit()
                else None
            )
            return ProviderTransientError(
                f"provider '{self.name}': 429 rate limited", retry_after_s=seconds
            )
        if 500 <= status <= 599:
            return ProviderTransientError(f"provider '{self.name}': {status} server error: {body}")
        if status in {401, 403, 404, 400}:
            return ProviderPermanentError(
                f"provider '{self.name}': {status}: {body}", http_status=status
            )
        return ProviderPermanentError(
            f"provider '{self.name}': unexpected HTTP {status}: {body}", http_status=status
        )

    async def ping(self) -> float:
        started = time.perf_counter()
        request = LLMRequest(
            system="You are a health check.",
            user="Reply with the single word: ok",
            max_tokens=8,
            expect_json=False,
        )
        await self.complete(request)
        return (time.perf_counter() - started) * 1000

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
