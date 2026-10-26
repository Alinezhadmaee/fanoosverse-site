"""LLM provider layer: request/response models, exceptions, provider ABC.

No vendor SDK, no model names, no vendor details outside ``llm/providers/``.
The rest of the system only ever sees :class:`LLMProvider`,
:class:`LLMRequest`, and :class:`LLMResponse`.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """Base class for every LLM-layer error."""


class ProviderTransientError(LLMError):
    """Retryable failure: 429, 5xx, timeout, connection error."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ProviderPermanentError(LLMError):
    """Non-retryable failure: 401/403 invalid key, 404 unknown model, 400 bad request."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class OutputQualityError(LLMError):
    """The provider answered but the answer is unusable (not JSON, wrong shape)."""


class AllProvidersFailedError(LLMError):
    """Every configured provider failed; carries per-provider reasons."""

    def __init__(self, reasons: dict[str, str]) -> None:
        detail = "; ".join(f"{name}: {reason}" for name, reason in reasons.items())
        super().__init__(f"all providers failed -> {detail}")
        self.reasons = reasons


class OutputQualityFailureError(LLMError):
    """All providers answered but every answer failed output-quality checks.

    Distinct from :class:`AllProvidersFailedError`: the record is
    ``needs_review``, not ``failed``.
    """

    def __init__(self, reasons: dict[str, str]) -> None:
        detail = "; ".join(f"{name}: {reason}" for name, reason in reasons.items())
        super().__init__(f"all providers returned unusable output -> {detail}")
        self.reasons = reasons


@dataclass(slots=True)
class LLMRequest:
    system: str
    user: str
    max_tokens: int = 2048
    expect_json: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    # Set by the router on a repair retry; providers must not need to know.
    repair_note: str | None = None


@dataclass(slots=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    provider: str
    model: str
    finish_reason: str | None = None
    cost_usd: float = 0.0
    cached: bool = False


class LLMProvider(ABC):
    """Interface every adapter implements. ``name`` is the configured name."""

    name: str = "base"
    model: str = ""

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute one completion request (transport failures raise)."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None

    @abstractmethod
    async def ping(self) -> float:
        """Tiny health-check request; returns latency in ms."""


_TOLERANT_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_json_tolerant(text: str) -> Any:
    """Parse a JSON object out of a model answer.

    Tolerates markdown fences and surrounding prose but is strict about the
    JSON itself. Raises :class:`OutputQualityError` when nothing parseable
    remains.
    """

    candidates: list[str] = [text.strip()]
    fence = _TOLERANT_JSON_RE.search(text)
    if fence is not None:
        candidates.insert(0, fence.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    raise OutputQualityError("answer is not parseable JSON")
