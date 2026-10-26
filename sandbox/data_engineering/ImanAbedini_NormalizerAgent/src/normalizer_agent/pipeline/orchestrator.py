"""Record orchestrator: intake -> deterministic -> vocab -> LLM -> validate -> assemble.

Produces the output envelope (schemas/envelope.py). One bad record never
stops a batch: every failure is captured in the envelope.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from normalizer_agent import PROMPT_VERSION, __version__
from normalizer_agent.cache.sqlite_cache import LLMCache
from normalizer_agent.llm.router import FallbackRouter
from normalizer_agent.observability.logging import log
from normalizer_agent.pipeline.collector import Collector, IntakeError, coerce_record
from normalizer_agent.pipeline.deterministic import DeterministicResolver
from normalizer_agent.pipeline.items import WorkItem
from normalizer_agent.pipeline.llm_stage import LLMStage, PromptRenderer
from normalizer_agent.review.sink import ReviewSink
from normalizer_agent.rules.dates import parse_date
from normalizer_agent.schemas import job_ad as schema
from normalizer_agent.schemas.envelope import (
    AuditEntry,
    NormalizeResult,
    RecordStatus,
    ReviewItem,
    UsageStats,
)
from normalizer_agent.settings import AppConfig, FieldRule
from normalizer_agent.text.placeholders import is_placeholder
from normalizer_agent.text.unify import unify_text
from normalizer_agent.validate import checks
from normalizer_agent.vocab.loader import VocabStore
from normalizer_agent.vocab.retriever import TrigramRetriever

logger = logging.getLogger(__name__)


class RecordNormalizer:
    def __init__(
        self,
        config: AppConfig,
        vocab_store: VocabStore,
        router: FallbackRouter,
        cache: LLMCache,
        review_sink: ReviewSink | None = None,
        retriever: TrigramRetriever | None = None,
    ) -> None:
        self.config = config
        self.vocab_store = vocab_store
        self.router = router
        self.cache = cache
        self.review_sink = review_sink
        self.collector = Collector(config, vocab_store)
        self.resolver = DeterministicResolver(retriever or TrigramRetriever())
        prompts_dir = config.config_dir.parent / "prompts"
        self.llm_stage = LLMStage(
            config=config,
            router=router,
            cache=cache,
            prompt_renderer=PromptRenderer(prompts_dir),
            vocab_versions=vocab_store.versions(),
            vocab_values={name: v.values() for name, v in vocab_store._vocabs.items()},
            retriever=retriever or TrigramRetriever(),
        )
        self.llm_stage.attach_store(vocab_store)

    # ------------------------------------------------------------------ #

    async def normalize(
        self,
        record: dict[str, Any],
        *,
        record_id: str | None = None,
        trace_id: str | None = None,
    ) -> NormalizeResult:
        versions: dict[str, str | dict[str, str]] = {
            "normalizer": __version__,
            "prompt": PROMPT_VERSION,
            "vocab": dict(self.vocab_store.versions()),
        }
        rid = record_id or str(record.get("document_id", "<unknown>"))

        try:
            raw_model = coerce_record(record)
        except IntakeError as exc:
            return NormalizeResult(
                record_id=rid,
                status=RecordStatus.REJECTED,
                error_codes=[exc.error_code],
                versions=dict(versions),
                trace_id=trace_id,
            )

        collected_at = _as_date(raw_model.collected_at)
        role_title = str(raw_model.role_title or "")

        items = self.collector.collect(raw_model)
        for item in items:
            vocab = self.vocab_store.get(item.rule.vocab) if item.rule.vocab else _EMPTY_RULES
            self.resolver.resolve(item, vocab, collected_at=collected_at)

        unresolved = [i for i in items if i.resolved is None and i.review_reason is None]
        usage = UsageStats()
        if unresolved:
            llm_result = await self.llm_stage.resolve(unresolved, role_title=role_title)
            usage = UsageStats(
                input_tokens=llm_result.input_tokens,
                output_tokens=llm_result.output_tokens,
                estimated_cost_usd=llm_result.cost_usd,
                llm_calls=llm_result.calls,
            )
            if llm_result.all_providers_failed:
                return NormalizeResult(
                    record_id=rid,
                    status=RecordStatus.FAILED,
                    error_codes=["ALL_PROVIDERS_FAILED"],
                    usage=usage,
                    versions=dict(versions),
                    trace_id=trace_id,
                )

        # dates (source_date is passthrough-provenance but the value must be ISO)
        global _VOCAB_VERSIONS
        _VOCAB_VERSIONS = self.vocab_store.versions()
        assembly = _Assembly(config=self.config, raw=raw_model, items=items, record_id=rid)
        result = assembly.build(usage=usage, versions=versions, trace_id=trace_id)
        for review in result.review_items:
            if self.review_sink is not None:
                self.review_sink.write(rid, review)
        log(
            logger,
            logging.INFO,
            "record normalized",
            record_id=rid,
            stage="done",
            status=result.status.value,
            audit_entries=len(result.audit),
            review_items=len(result.review_items),
            llm_calls=usage.llm_calls,
        )
        return result


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        parsed, _ = parse_date(value)
        return parsed
    return None


_EMPTY_RULES: Any = None
_VOCAB_VERSIONS: dict[str, str] = {}


class _Assembly:
    """Builds the strict JobAd from resolved items; degrades, never crashes."""

    def __init__(
        self,
        config: AppConfig,
        raw: schema.JobAdRaw,
        items: list[WorkItem],
        record_id: str | None = None,
    ) -> None:
        self.config = config
        self.raw = raw
        self.items = items
        self.record_id = record_id or raw.document_id
        self.audit: list[AuditEntry] = []
        self.reviews: list[ReviewItem] = []
        self.evidence_extra: list[str] = []
        self.candidates_new: list[str] = []
        self.vocab_store_version: dict[str, str] = _VOCAB_VERSIONS

    def build(
        self,
        *,
        usage: UsageStats,
        versions: dict[str, str | dict[str, str]],
        trace_id: str | None,
    ) -> NormalizeResult:
        fields: dict[str, Any] = {}
        warnings: list[str] = []

        by_field: dict[str, list[WorkItem]] = {}
        for item in self.items:
            by_field.setdefault(item.field, []).append(item)

        for field_name, rule in self.config.fields.fields.items():
            try:
                value = self._build_field(field_name, rule, by_field.get(field_name, []))
                fields[field_name] = value
            except Exception as exc:  # noqa: BLE001 - degrade, never crash assembly
                warnings.append(f"{field_name}: {exc}")
                fields[field_name] = None

        # provenance / passthrough
        fields.update(self._passthrough())

        # money currency
        salary_items = by_field.get("salary_min", [])
        currency: str | None = "IRR"
        for item in salary_items:
            if item.extra.get("negotiable"):
                currency = None
                self.evidence_extra.append("salary: negotiable")
            elif item.extra.get("currency"):
                currency = item.extra["currency"]
        fields["salary_currency"] = (
            currency
            if fields.get("salary_min") is not None or fields.get("salary_max") is not None
            else None
        )

        # free-text lists: append-only additions from the normalizer
        fields["evidence_notes"] = self._list(raw_value=self.raw.evidence_notes) + sorted(
            set(self.evidence_extra)
        )
        fields["candidate_new_skill"] = self._list(raw_value=self.raw.candidate_new_skill) + sorted(
            set(self.candidates_new)
        )

        try:
            strict = schema.JobAd.model_validate(fields)
        except Exception:  # noqa: BLE001
            return NormalizeResult(
                record_id=self.record_id,
                status=RecordStatus.NEEDS_REVIEW,
                error_codes=["VALIDATION_FAILED"],
                review_items=self.reviews,
                audit=self.audit,
                usage=usage,
                versions=versions,
                trace_id=trace_id,
            )

        status = RecordStatus.NEEDS_REVIEW if self.reviews else RecordStatus.NORMALIZED
        return NormalizeResult(
            record_id=self.record_id,
            status=status,
            record=strict,
            error_codes=[],
            review_items=self.reviews,
            audit=self.audit,
            usage=usage,
            versions=versions,
            trace_id=trace_id,
        )

    # ------------------------------------------------------------------ #

    def _build_field(self, field_name: str, rule: FieldRule, items: list[WorkItem]) -> Any:
        kind = rule.kind
        if kind == "passthrough":
            return getattr(self.raw, field_name, None)
        if kind in {"vocab_single", "enum_single", "location"}:
            resolved = [i for i in items if i.resolved]
            if resolved:
                self._audit(resolved[0])
                return resolved[0].resolved
            for item in items:
                self._review(item)
            return None
        if kind in {"vocab_list", "enum_list"}:
            values: list[str] = []
            for item in items:
                if item.resolved:
                    values.append(item.resolved)
                    self._audit(item)
                else:
                    self._review(item)
            clean, problems = checks.list_hygiene(values)
            for problem in problems:
                self.evidence_extra.append(f"{field_name}: {problem}")
            if field_name in schema.ENUM_FIELDS:
                enum_cls = schema.ENUM_FIELDS[field_name]
                valid: list[str] = []
                for value in clean:
                    if checks.member_of_enum(value, enum_cls):
                        valid.append(value)
                    else:
                        self.evidence_extra.append(f"{field_name}: dropped non-member {value}")
                return valid
            return clean
        if kind == "integer":
            resolved = [i for i in items if i.resolved is not None]
            if resolved:
                item = resolved[0]
                self._audit(item)
                assert item.resolved is not None
                value_int = int(item.resolved)
                problems = checks.numeric_sanity(
                    min_value=value_int, max_value=None, value_range=rule.value_range
                )
                if problems:
                    for problem in problems:
                        self.evidence_extra.append(f"{field_name}: {problem}")
                return value_int
            for item in items:
                self._review(item)
            return None
        if kind in {"integer_range_min", "integer_range_max"}:
            value_range = self._range_bound(field_name, rule, items)
            return value_range
        if kind == "money":
            if not items and rule.pair:
                items = [i for i in self.items if i.field == rule.pair]
            return self._money_field(field_name, items)
        if kind in {"money_currency", "free_text_list"}:
            return None  # filled elsewhere
        if kind == "date":
            return None
        return None

    def _range_bound(self, field_name: str, rule: FieldRule, items: list[WorkItem]) -> int | None:
        """Age/company-size pair: one side may hold a full range."""

        partner_items: list[WorkItem] = []
        if rule.pair:
            partner_name = rule.pair
            # items were collected for every field; partner items exist too
            partner_items = [
                i for i in self.items if i.field == partner_name and i.resolved is not None
            ]
        own = [i for i in items if i.resolved is not None]
        own_values = [int(i.resolved) for i in own if i.resolved is not None]
        partner_values = [int(i.resolved) for i in partner_items if i.resolved is not None]

        from normalizer_agent.rules.numbers import extract_range

        all_values = own_values + partner_values
        for item in own + partner_items:
            low, high = extract_range(item.raw)
            if low is not None and high is not None:
                all_values.extend([low, high])
        if not all_values:
            for item in items + partner_items:
                self._review(item)
            return None
        value = min(all_values) if field_name.endswith("_min") else max(all_values)
        problems = checks.numeric_sanity(
            min_value=value, max_value=None, value_range=rule.value_range
        )
        for problem in problems:
            self.evidence_extra.append(f"{field_name}: {problem}")
        return value

    def _money_field(self, field_name: str, items: list[WorkItem]) -> int | None:
        item = items[0] if items else None
        if item is None or item.resolved is None:
            return None
        extra = item.extra
        if extra.get("currency") != "IRR":
            return extra.get("amount")
        key = "min" if field_name.endswith("_min") else "max"
        value = extra.get(key)
        if value is None and field_name.endswith("_max"):
            value = extra.get("min") if extra.get("max") is None else None
        if value is not None:
            problem = checks.salary_plausibility(
                value,
                low=self.config.pipeline.numeric.salary_min_irr_warn,
                high=self.config.pipeline.numeric.salary_max_irr_warn,
            )
            if problem:
                self.evidence_extra.append(f"{field_name}: {problem}")
        return value

    def _passthrough(self) -> dict[str, Any]:
        raw = self.raw
        source_type = raw.source_type
        if isinstance(source_type, str) and source_type:
            st = (
                schema.SourceType(source_type)
                if source_type in schema.SourceType._value2member_map_
                else schema.SourceType.JOB_POSTING
            )
        else:
            st = schema.SourceType.JOB_POSTING
        return {
            "document_id": raw.document_id,
            "collector": raw.collector,
            "source_type": st,
            "role_title": _scalar_str(raw.role_title),
            "text": raw.text,
            "content_hash": raw.content_hash,
            "version": raw.version,
            "source_date": _as_date(raw.source_date),
            "collected_at": _as_date(raw.collected_at),
        }

    def _list(self, *, raw_value: Any) -> list[str]:
        from normalizer_agent.pipeline.collector import _raw_list

        out = [unify_text(v) for v in _raw_list(raw_value)]
        return [v for v in out if v and not is_placeholder(v)]

    def _audit(self, item: WorkItem) -> None:
        note = "; ".join(item.removed) if item.removed else item.note
        if self.config.pipeline.record_raw_in_evidence_notes and item.removed:
            self.evidence_extra.append(
                f"{item.field}: removed '{'; '.join(item.removed)}' from '{item.raw}'"
            )
        self.audit.append(
            AuditEntry(
                field=item.field,
                index=_int_or_none(item.index),
                raw=item.raw,
                canonical=item.resolved,
                method=item.method,
                confidence=item.confidence,
                provider=item.provider,
                model=item.model,
                vocab_version=self.vocab_version(item),
                note=note,
            )
        )
        if item.candidate_new and item.resolved:
            self.candidates_new.append(item.resolved)

    def _review(self, item: WorkItem) -> None:
        reason = item.review_reason or "not_in_vocabulary"
        self.reviews.append(
            ReviewItem(
                field=item.field,
                index=_int_or_none(item.index),
                raw=item.raw,
                proposed=item.resolved,
                reason=reason,
            )
        )

    def vocab_version(self, item: WorkItem) -> str | None:
        if item.rule.vocab:
            return self.vocab_store_version.get(item.rule.vocab)
        return None

    vocab_store_version: dict[str, str] = {}


def _int_or_none(index: int | str | None) -> int | None:
    if isinstance(index, int):
        return index
    if isinstance(index, str) and index.split(".")[0].isdigit():
        return int(index.split(".")[0])
    return None


def _scalar_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return unify_text(" ".join(str(v) for v in value))
    return unify_text(str(value))
