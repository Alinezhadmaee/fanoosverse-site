"""Mock provider: no network, deterministic answers.

Two ways to control it (via ``params`` in llm.yaml or directly in code):

- ``scripted``: a list of raw answer texts, popped per call. After the list
  is exhausted, every call fails with a transient error.
- ``behavior: canonical`` (default): the provider *simulates a good model*
  for the normalizer payload: it reads the JSON payload embedded in the user
  prompt, resolves each item through the (mock-local) canonical map and the
  shipped vocabularies, and answers in the exact required shape. Values it
  cannot resolve are answered with ``null`` — never a guess.

Fault injection for tests, via ``params.fail_next``:
``["5xx"]``, ``"429"``, ``"401"``, ``"invalid_json"``, ``"timeout"``,
``"empty"`` — consumed once per occurrence.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from normalizer_agent.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    OutputQualityError,
    ProviderPermanentError,
    ProviderTransientError,
    parse_json_tolerant,
)
from normalizer_agent.llm.registry import register, resolve_api_key
from normalizer_agent.rules.naming import title_case
from normalizer_agent.settings import ProviderConfig
from normalizer_agent.text.unify import unify_key
from normalizer_agent.vocab.loader import VocabStore

# Small deterministic "model knowledge" table used by behavior=canonical.
_BUILTIN: dict[str, str] = {
    "پایتون": "Python",
    "py": "Python",
    "python3": "Python",
    "یادگیری ماشین": "Machine Learning",
    "هوش مصنوعی": "Machine Learning",
    "تمام وقت": "full_time",
    "تماموقت": "full_time",
    "پاره وقت": "part_time",
    "دورکاری": "remote",
    "حضوری": "onsite",
    "کارآموزی": "internship",
    "توافقی": None,  # type: ignore[dict-item]
    "نامشخص": None,  # type: ignore[dict-item]
}

_ENUM_LIKE = {"enum_list", "enum_single", "vocab_list", "vocab_single", "location"}


@register("mock")
def factory(config: ProviderConfig) -> LLMProvider:
    return MockProvider(config)


class MockProvider(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.name = config.name
        self.model = config.model
        self._config = config
        self._api_key = resolve_api_key(config)
        params: dict[str, Any] = dict(config.params or {})
        self._scripted: list[str] = list(params.get("scripted", []))
        self._behavior: str = str(params.get("behavior", "canonical"))
        self._fail_next: list[str] = list(params.get("fail_next", []))
        self._latency_ms: float = float(params.get("latency_ms", 5))
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        await asyncio.sleep(0)  # yield, keeps async semantics honest
        started = time.perf_counter()

        if self._fail_next:
            fault = self._fail_next.pop(0)
            self._raise_fault(fault)

        if self._scripted:
            text = self._scripted.pop(0)
        elif self._behavior == "canonical":
            text = self._canonical_answer(request)
        else:
            raise ProviderTransientError(f"mock '{self.name}': exhausted and no behavior")

        latency_ms = (time.perf_counter() - started) * 1000 + self._latency_ms
        return LLMResponse(
            text=text,
            input_tokens=len(request.system.split()) + len(request.user.split()),
            output_tokens=len(text.split()),
            latency_ms=latency_ms,
            provider=self.name,
            model=self.model,
            finish_reason="end_turn",
            cost_usd=0.0,
        )

    def _raise_fault(self, fault: str) -> None:
        faults: dict[str, Callable[[], Exception]] = {
            "5xx": lambda: ProviderTransientError("mock 5xx"),
            "timeout": lambda: ProviderTransientError("mock timeout"),
            "429": lambda: ProviderTransientError("mock 429", retry_after_s=0.01),
            "401": lambda: ProviderPermanentError("mock 401 invalid key", http_status=401),
            "empty": lambda: ProviderTransientError("mock empty answer"),
        }
        if fault == "invalid_json":
            self._scripted.insert(0, "this is { not json")
            return
        if fault == "429_no_header":
            raise ProviderTransientError("mock 429")
        if fault in faults:
            raise faults[fault]()
        raise ProviderTransientError(f"mock unknown fault '{fault}'")

    def _canonical_answer(self, request: LLMRequest) -> str:
        try:
            payload = parse_json_tolerant(request.user)
        except OutputQualityError as exc:
            raise ProviderPermanentError("mock: no payload found in prompt") from exc
        items = payload.get("items", [])
        candidates: dict[str, list[str]] = payload.get("candidates", {})
        answers = []
        store = self._vocab_store()
        for item in items:
            field = item.get("field", "")
            kind = item.get("kind", "vocab_list")
            raw = item.get("raw_value", "")
            key = unify_key(raw)
            answer: str | None = None
            # 1) candidates offered by the pipeline
            for candidate in candidates.get(field, []):
                if unify_key(candidate) == key:
                    answer = candidate
                    break
            # 2) the vocabulary itself
            if answer is None:
                rule = self._vocab_name(field)
                if rule:
                    hit = store.get(rule).resolve(raw)
                    if hit is not None:
                        answer = hit[0].value
            # 3) tiny builtin table
            if answer is None and key in _BUILTIN:
                answer = _BUILTIN[key]
            # 4) heuristic: already-Latin values pass through with naming fixes
            if answer is None and raw.isascii():
                answer = title_case(raw) if kind not in {"enum_list", "enum_single"} else None
            answers.append(
                {
                    "field": field,
                    "index": item.get("index", 0),
                    "canonical": [answer] if answer else None,
                    "category": None,
                    "confidence": "high" if answer else "low",
                    "reason": "mock",
                }
            )

        return json.dumps({"items": answers}, ensure_ascii=False)

    def _vocab_store(self) -> VocabStore:
        from normalizer_agent.vocab.loader import VocabStore

        cached = getattr(self, "_store", None)
        if cached is None:
            vocab_dir = Path(__file__).resolve().parents[3] / "config" / "vocab"
            self._store = VocabStore(vocab_dir)
        return self._store

    _FIELD_VOCAB = {
        "seniority": "seniority",
        "skills_required": "capability",
        "skills_preferred": "capability",
        "ai_skills": "capability",
        "tools": "capability",
        "soft_skills": "capability",
        "benefits": "benefit",
        "education": "education",
        "education_field": "education_field",
        "language": "language",
        "organization": "organization",
        "role_normalized": "role",
        "domain": "domain",
        "industry": "industry",
        "compensation_type": "compensation_type",
        "employment_type": "employment_type",
        "work_mode": "work_mode",
        "education_degree": "education_degree",
        "military_service_requirement": "military_service_requirement",
        "gender_requirement": "gender_requirement",
        "location": "location",
    }

    def _vocab_name(self, field: str) -> str | None:
        return self._FIELD_VOCAB.get(field)

    async def ping(self) -> float:
        started = time.perf_counter()
        await self.complete(LLMRequest(system="health", user="ok", max_tokens=8, expect_json=False))
        return (time.perf_counter() - started) * 1000
