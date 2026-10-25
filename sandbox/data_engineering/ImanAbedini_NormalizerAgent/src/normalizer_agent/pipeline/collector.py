"""Stage 1+2: intake, structural validation, deterministic preprocessing.

Builds :class:`WorkItem`s from the raw record: char unification, placeholder /
explicit-no detection, parenthetical removal, composite splitting — everything
that needs no LLM judgment.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from normalizer_agent.pipeline.items import WorkItem
from normalizer_agent.schemas import job_ad as schema
from normalizer_agent.settings import AppConfig, FieldRule
from normalizer_agent.text.composite import split_composite
from normalizer_agent.text.parens import strip_parentheticals
from normalizer_agent.text.placeholders import is_explicit_no, is_placeholder
from normalizer_agent.text.unify import unify_key, unify_text
from normalizer_agent.vocab.loader import VocabStore


class IntakeError(Exception):
    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


def coerce_record(record: dict[str, Any]) -> schema.JobAdRaw:
    """Validate the raw record shape; raises IntakeError (UNKNOWN_KEY / SCHEMA_INVALID)."""

    try:
        return schema.JobAdRaw.model_validate(record)
    except Exception as exc:
        raise IntakeError(schema.classify_raw_validation_error(exc), str(exc)) from exc


def _raw_list(value: Any) -> list[str]:
    """Coerce an arbitrary raw field value into a list of strings."""

    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)):
        # integers that arrived as numbers skip string noise
        return [str(int(value)) if float(value).is_integer() else str(value)]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_raw_list(item))
        return out
    return [str(value)]


def _money_raw(raw_model: schema.JobAdRaw, rule: FieldRule, own: str) -> str:
    """Combine the salary pair's raw texts into one parseable string.

    The pair fields (salary_min/salary_max) each carry part of the story:
    "38 to 52 million" / "52 million toman". Joining them lets the money rule
    see the full range once.
    """

    parts: list[str] = []
    for name in (own, rule.pair):
        if name is None:
            continue
        value = getattr(raw_model, name, None)
        if value is None:
            continue
        for piece in _raw_list(value):
            piece = unify_text(piece).strip()
            if piece and not is_placeholder(piece) and piece not in parts:
                parts.append(piece)
    return " ".join(parts) if parts else ""


class Collector:
    def __init__(self, config: AppConfig, vocab_store: VocabStore) -> None:
        self.config = config
        self.vocab_store = vocab_store
        self.whitelist = {unify_key(w) for w in config.pipeline.composite_whitelist}

    def collect(self, raw_model: schema.JobAdRaw) -> list[WorkItem]:
        """All WorkItems for one record, excluding provenance passthrough."""

        items: list[WorkItem] = []
        for field_name, rule in self.config.fields.fields.items():
            if rule.kind in {"passthrough", "money_currency", "free_text_list"}:
                continue  # handled at assembly
            raw_value = getattr(raw_model, field_name, None)
            if raw_value is None or raw_value == "":
                continue
            if rule.kind == "money":
                # salary_min/max share one parse; only the primary side collects
                if field_name != self._primary_money_field(rule):
                    continue
                raw_for_money = _money_raw(raw_model, rule, field_name)
                if raw_for_money:
                    items.append(
                        WorkItem(field=field_name, index=None, raw=raw_for_money, rule=rule)
                    )
                continue
            scalar = not isinstance(raw_value, list)
            for position, value in enumerate(_raw_list(raw_value)):
                index = None if scalar else position
                items.extend(self._explode(field_name, index, value, rule))
        return items

    def _primary_money_field(self, rule: FieldRule) -> str:
        money_fields = [
            name for name, other in self.config.fields.fields.items() if other.kind == "money"
        ]
        for name in money_fields:
            if name.endswith("_min"):
                return name
        return money_fields[0] if money_fields else "salary_min"

    def _explode(
        self, field: str, index: int | None, value: str, rule: FieldRule
    ) -> list[WorkItem]:
        """Preprocess one raw string into zero or more WorkItems."""

        cleaned, removed = strip_parentheticals(unify_text(value))
        stripped = value.strip()
        if not stripped or is_placeholder(stripped):
            return []  # R-placeholder: never emitted (drop policy)
        if is_explicit_no(stripped) and rule.explicit_no_member:
            item = WorkItem(
                field=field,
                index=index,
                raw=stripped,
                rule=rule,
                resolved=rule.explicit_no_member,
                method="rule",
                confidence="high",
                removed=removed,
                note="explicit_no",
            )
            return [item]

        # whole-value vocabulary hit -> never split (protects legit composites)
        keep_whole = self._whole_lookup(field, rule)
        parts = [stripped]
        if rule.allow_composite_split and len(strip_parentheticals(stripped)[0]) > 0:
            parts = split_composite(cleaned, self.whitelist, keep_whole)

        out: list[WorkItem] = []
        for position, part in enumerate(parts):
            part_cleaned, part_removed = strip_parentheticals(part)
            if not part_cleaned or is_placeholder(part_cleaned):
                continue
            out.append(
                WorkItem(
                    field=field,
                    index=index
                    if len(parts) == 1
                    else (None if index is None else f"{index}.{position}"),
                    raw=part_cleaned,
                    rule=rule,
                    removed=removed + part_removed,
                )
            )
        return out

    def _whole_lookup(self, field: str, rule: FieldRule) -> Callable[[str], bool] | None:
        """Return a callable telling split_composite when the whole value is known."""

        if not rule.vocab:
            return None
        vocab = self.vocab_store.get(rule.vocab)

        def keep(whole_key: str) -> bool:
            return vocab.resolve(whole_key) is not None

        return keep
