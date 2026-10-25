"""OpenAI-compatible Chat Completions adapter.

Works for OpenAI, Qwen/DashScope, GLM, DeepSeek, Gemini-compat endpoints,
vLLM, Ollama and any server implementing ``/chat/completions``.
"""

from __future__ import annotations

import asyncio
import random
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


@register("openai_compat")
def factory(config: ProviderConfig) -> LLMProvider:
    return OpenAICompatProvider(config)


class OpenAICompatProvider(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.name = config.name
        self.model = config.model
        self._config = config
        self._api_key = resolve_api_key(config)
        if not config.base_url:
            raise ProviderPermanentError(
                f"provider '{config.name}': base_url is required for openai_compat"
            )
        self._base_url = config.base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"content-type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._config.timeout_s,
                headers=headers,
            )
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._ensure_client()
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.user})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        # sampling params are sent ONLY when configured for this provider
        for key, value in self._config.params.items():
            payload.setdefault(key, value)
        if self._config.features.json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        response = await self._request_with_retries(client, "/chat/completions", payload)
        latency_ms = (time.perf_counter() - started) * 1000
        data = response.json()
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)
        return LLMResponse(
            text=message.get("content") or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            provider=self.name,
            model=data.get("model", self.model),
            finish_reason=choices[0].get("finish_reason"),
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
            f"provider '{self.name}': unexpected HTTP {status}: {body}",
            http_status=status,
        )

    async def ping(self) -> float:
        started = time.perf_counter()
        await self.complete(
            LLMRequest(
                system="You are a health check.",
                user="Reply with the single word: ok",
                max_tokens=8,
                expect_json=False,
            )
        )
        return (time.perf_counter() - started) * 1000

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
