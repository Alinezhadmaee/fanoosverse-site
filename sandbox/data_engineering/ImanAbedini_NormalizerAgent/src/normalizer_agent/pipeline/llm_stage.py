"""Stage 4: LLM resolution — one request per record, grouped by field.

Modes: direct | candidates | full_list (pipeline.yaml ``llm_mode``).
The parser is tolerant (fences/prose) but strict about shape; post-LLM
validation is deterministic (validate/checks.py). Cache hits skip the LLM.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from normalizer_agent.cache.sqlite_cache import LLMCache, cache_key
from normalizer_agent.llm.base import (
    AllProvidersFailedError,
    LLMRequest,
    LLMResponse,
    OutputQualityError,
    OutputQualityFailureError,
    parse_json_tolerant,
)
from normalizer_agent.llm.router import FallbackRouter
from normalizer_agent.pipeline.items import Confidence, Method, WorkItem
from normalizer_agent.schemas import job_ad as schema
from normalizer_agent.settings import AppConfig
from normalizer_agent.validate.checks import member_of_enum, naming_format_ok
from normalizer_agent.vocab.loader import VocabStore
from normalizer_agent.vocab.retriever import TrigramRetriever

logger = logging.getLogger(__name__)

PROMPTS_DIR_NAME = "prompts"
NORMALIZE_PROMPT = "normalize_values.v1.md"
REPAIR_PROMPT = "repair_output.v1.md"

_ALLOWED_ANSWER_KEYS = {"field", "index", "canonical", "category", "confidence", "reason"}


def load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class PromptRenderer:
    """Tiny ``{{var}}`` renderer; prompts are files, never inline strings."""

    def __init__(self, prompts_dir: Path) -> None:
        self.dir = prompts_dir
        self._cache: dict[str, str] = {}

    def render(self, name: str, **variables: str) -> tuple[str, str]:
        template = self._cache.get(name)
        if template is None:
            template = load_prompt(self.dir / name)
            self._cache[name] = template
        text = template
        for key, value in variables.items():
            text = text.replace("{{" + key + "}}", value)
        return template, text


@dataclass(slots=True)
class LLMItemAnswer:
    field: str
    index: Any
    canonical: list[str] | None
    category: str | None
    confidence: Confidence
    reason: str


@dataclass
class LLMStageResult:
    responses: list[LLMResponse] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    all_providers_failed: bool = False
    quality_failure: bool = False


class LLMStage:
    def __init__(
        self,
        config: AppConfig,
        router: FallbackRouter,
        cache: LLMCache,
        prompt_renderer: PromptRenderer,
        vocab_versions: dict[str, str],
        vocab_values: dict[str, list[str]],
        retriever: TrigramRetriever,
    ) -> None:
        self.config = config
        self.router = router
        self.cache = cache
        self.renderer = prompt_renderer
        self.vocab_versions = vocab_versions
        self.vocab_values = vocab_values
        self.retriever = retriever
        self._store: VocabStore | None = None

    # ----------------------------- request ----------------------------- #

    def build_user_prompt(self, items: list[WorkItem], role_title: str) -> str:
        payload_items: list[dict[str, Any]] = []
        candidates_block: dict[str, list[str]] = {}
        vocab_block: dict[str, dict[str, Any]] = {}
        mode = self.config.pipeline.llm_mode
        for item in items:
            entry: dict[str, Any] = {
                "field": item.field,
                "index": _display_index(item.index),
                "raw_value": item.raw,
                "kind": item.rule.kind,
            }
            if item.rule.is_skill or item.rule.kind in {"vocab_list", "vocab_single", "location"}:
                entry["context"] = {"role_title": role_title}
            payload_items.append(entry)

            if mode == "candidates" and item.rule.vocab and self._store is not None:
                vocab = self._store.get(item.rule.vocab)
                scored = self.retriever.retrieve(
                    vocab,
                    item.raw,
                    self.config.pipeline.candidates_top_k,
                )
                if scored:
                    candidates_block.setdefault(item.field, [])
                    candidates_block[item.field].extend(
                        e.value for e, _ in scored if e.value not in candidates_block[item.field]
                    )
            elif mode == "full_list" and item.rule.vocab:
                values = self.vocab_values.get(item.rule.vocab, [])
                if values and item.field not in vocab_block:
                    vocab_block[item.field] = {
                        "values": values[: self.config.pipeline.full_list_max_values],
                        "kind": item.rule.kind,
                    }

        payload: dict[str, Any] = {"items": payload_items}
        if candidates_block:
            payload["candidates"] = candidates_block
        if vocab_block:
            payload["vocab"] = vocab_block
        return json.dumps(payload, ensure_ascii=False, indent=1)

    # ------------------------------ flow ------------------------------- #

    async def resolve(self, items: list[WorkItem], *, role_title: str) -> LLMStageResult:
        result = LLMStageResult()
        pending: list[WorkItem] = []

        for item in items:
            key = cache_key(
                field_kind=item.rule.kind,
                raw_value=item.raw,
                vocab_version=self.vocab_versions.get(item.rule.vocab or "", "empty"),
                llm_mode=self.config.pipeline.llm_mode,
                key_includes_model=self.config.pipeline.cache.key_includes_model,
            )
            entry = self.cache.get(key)
            if entry is not None:
                _apply_answer(
                    item,
                    LLMItemAnswer(
                        field=item.field,
                        index=item.index,
                        canonical=entry.canonical,
                        category=entry.category,
                        confidence=entry.confidence
                        if entry.confidence in {"high", "low"}
                        else "low",
                        reason=entry.reason,
                    ),
                    method="cache",
                    provider=entry.provider,
                    model=entry.model,
                )
                item.extra["cache_key"] = key
            else:
                item.extra["cache_key"] = key
                pending.append(item)

        if not pending:
            return result

        user_prompt = self.build_user_prompt(pending, role_title)
        _, system = self.renderer.render(NORMALIZE_PROMPT)
        request = LLMRequest(
            system=system,
            user=user_prompt,
            max_tokens=self.config.pipeline.llm_max_tokens,
            expect_json=True,
        )

        try:
            response = await self.router.complete(request)
        except AllProvidersFailedError:
            result.all_providers_failed = True
            return result
        except OutputQualityFailureError:
            result.quality_failure = True
            return result

        result.responses.append(response)
        self._account(result, response)

        parsed = parse_json_tolerant(response.text)
        answers, error = self._validate_shape(parsed, pending)
        if error is not None:
            # one repair attempt through the router (same provider first)
            repaired = await self._repair(request, response, error)
            if repaired is None:
                result.quality_failure = True
                return result
            response = repaired
            result.responses.append(response)
            self._account(result, response)
            parsed = parse_json_tolerant(response.text)
            answers, error = self._validate_shape(parsed, pending)
            if error is not None:
                result.quality_failure = True
                return result

        for item in pending:
            answer = answers.get(_answer_key(item.field, _display_index(item.index)))
            if answer is None:
                item.review_reason = "validation_failed"
                item.note = "missing from LLM answer"
                continue
            _apply_answer(
                item, answer, method="llm", provider=response.provider, model=response.model
            )
            self._post_validate(item)
            self._store_cache(item)
        return result

    async def _repair(
        self, original: LLMRequest, bad: LLMResponse, error: str
    ) -> LLMResponse | None:
        _, system = self.renderer.render(REPAIR_PROMPT)
        user = (
            original.user
            + f"\n\n### VALIDATION ERROR\n{error}\n\n### YOUR PREVIOUS ANSWER\n{bad.text[:4000]}"
        )
        request = LLMRequest(
            system=system,
            user=user,
            max_tokens=original.max_tokens,
            expect_json=True,
        )
        try:
            response = await self.router.complete(request)
            parse_json_tolerant(response.text)
            return response
        except (AllProvidersFailedError, OutputQualityFailureError, OutputQualityError):
            return None

    # --------------------------- validation ---------------------------- #

    def _validate_shape(
        self, parsed: dict[str, Any], items: list[WorkItem]
    ) -> tuple[dict[tuple[str, Any], LLMItemAnswer], str | None]:
        raw_items = parsed.get("items")
        if not isinstance(raw_items, list):
            return {}, "answer.items must be a list"
        answers: dict[tuple[str, Any], LLMItemAnswer] = {}
        for entry in raw_items:
            if not isinstance(entry, dict):
                return {}, "answer.items entries must be objects"
            unknown = set(entry) - _ALLOWED_ANSWER_KEYS
            if unknown:
                return {}, f"unknown answer keys: {sorted(unknown)}"
            field = entry.get("field")
            if not isinstance(field, str):
                return {}, "answer item missing 'field'"
            canonical = entry.get("canonical")
            if canonical is not None and not (
                isinstance(canonical, list)
                and canonical
                and all(isinstance(v, str) for v in canonical)
            ):
                return {}, f"canonical must be a non-empty list of strings or null ({field})"
            confidence = entry.get("confidence")
            if confidence not in {"high", "low"}:
                return {}, f"confidence must be high|low ({field})"
            answers[(field, entry.get("index"))] = LLMItemAnswer(
                field=field,
                index=entry.get("index"),
                canonical=canonical,
                category=entry.get("category"),
                confidence=confidence,
                reason=str(entry.get("reason") or "")[:120],
            )
        expected = {(i.field, _display_index(i.index)) for i in items}
        missing = expected - set(answers)
        if missing:
            return answers, f"missing answer entries: {sorted(missing)[:5]}"
        return answers, None

    def _post_validate(self, item: WorkItem) -> None:
        """Deterministic checks on one LLM proposal; may flip to review."""

        if item.resolved is None:
            if item.review_reason is None:
                item.review_reason = (
                    "low_confidence" if item.confidence == "low" else "not_in_vocabulary"
                )
            return
        canonical = item.resolved
        enum_cls = schema.ENUM_FIELDS.get(item.field)
        if item.rule.kind in {"enum_list", "enum_single"} and enum_cls is not None:
            if not member_of_enum(canonical, enum_cls):
                item.resolved = None
                item.review_reason = "validation_failed"
                item.note = f"not a member of {enum_cls.__name__}"
                return
        elif item.rule.kind in {"vocab_list", "vocab_single", "location"}:
            vocab = (
                self._store.get(item.rule.vocab)
                if item.rule.vocab and self._store is not None
                else None
            )
            in_vocab = vocab is not None and vocab.resolve(canonical) is not None
            if not naming_format_ok(canonical, is_enum=False):
                item.resolved = None
                item.review_reason = "validation_failed"
                item.note = "naming format rejected"
                return
            if not in_vocab:
                # R-unknown-vocabulary: never silently accepted
                if (
                    item.confidence == "high"
                    and self.config.pipeline.min_accept_confidence == "high"
                ):
                    item.candidate_new = True
                    item.review_reason = "not_in_vocabulary"
                else:
                    item.resolved = None
                    item.review_reason = "low_confidence"
                return
        item.confidence = item.confidence or "low"

    def _store_cache(self, item: WorkItem) -> None:
        key = item.extra.get("cache_key")
        if not key:
            return
        self.cache.put(
            key,
            canonical=[item.resolved] if item.resolved else None,
            category=item.extra.get("category"),
            confidence=item.confidence or "low",
            reason=item.note or "llm",
            provider=item.provider or "",
            model=item.model or "",
        )

    def attach_store(self, store: VocabStore) -> None:
        self._store = store

    def _account(self, result: LLMStageResult, response: LLMResponse) -> None:
        result.input_tokens += response.input_tokens
        result.output_tokens += response.output_tokens
        result.cost_usd += response.cost_usd
        result.calls += 1


def _display_index(index: int | str | None) -> int | str | None:
    if isinstance(index, str) and "." in index:
        head = index.split(".")[0]
        return int(head) if head.isdigit() else index
    return index


def _answer_key(field: str, index: int | str | None) -> tuple[str, int | str | None]:
    return (field, index)


def _apply_answer(
    item: WorkItem,
    answer: LLMItemAnswer,
    *,
    method: Method,
    provider: str | None,
    model: str | None,
) -> None:
    item.method = method
    item.provider = provider
    item.model = model
    item.confidence = answer.confidence
    item.note = answer.reason
    if answer.canonical:
        item.resolved = answer.canonical[0]
    else:
        item.resolved = None
