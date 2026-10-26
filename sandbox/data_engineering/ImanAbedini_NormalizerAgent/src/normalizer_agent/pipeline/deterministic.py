"""Stages 2-3 (deterministic): rules + vocabulary resolution.

Deterministic code handles everything it can; only items that survive this
stage unresolved reach the LLM (one request per record).
"""

from __future__ import annotations

from datetime import date

from normalizer_agent.pipeline.items import WorkItem
from normalizer_agent.rules.integers import (
    extract_age,
    extract_experience_years,
    extract_integer,
    extract_months,
)
from normalizer_agent.rules.money import parse_money
from normalizer_agent.rules.naming import strip_version
from normalizer_agent.vocab.loader import Vocabulary
from normalizer_agent.vocab.retriever import CandidateRetriever


class DeterministicResolver:
    def __init__(self, retriever: CandidateRetriever) -> None:
        self.retriever = retriever

    def resolve(self, item: WorkItem, vocab: Vocabulary, *, collected_at: date | None) -> None:
        """Resolve ``item`` in place; unresolved items keep ``resolved=None``."""

        handler = {
            "vocab_list": self._vocab,
            "vocab_single": self._vocab,
            "enum_list": self._vocab,
            "enum_single": self._vocab,
            "location": self._vocab,
            "integer": self._integer,
            "integer_range_min": self._integer,
            "integer_range_max": self._integer,
            "money": self._money,
        }.get(item.rule.kind)
        if handler is None:
            return
        handler(item, vocab, collected_at)

    # ------------------------------------------------------------------ #

    def _vocab(self, item: WorkItem, vocab: Vocabulary, collected_at: date | None) -> None:
        hit = vocab.resolve(item.raw)
        if hit is None:
            # version/level suffixes are not part of the canonical name
            base, removed = strip_version(item.raw)
            if removed:
                hit = vocab.resolve(base)
                if hit is not None:
                    item.removed.append(removed)
        if hit is not None:
            item.resolved = hit[0].value
            item.method = "lookup"
            item.confidence = "high"
            if hit[0].category:
                item.extra["category"] = hit[0].category

    def _integer(self, item: WorkItem, vocab: Vocabulary, collected_at: date | None) -> None:
        if item.field == "experience_years_min":
            value = extract_experience_years(item.raw)
        elif item.field == "internship_duration_months":
            value = extract_months(item.raw)
        elif item.field.startswith("age_"):
            prefer = "max" if item.field.endswith("_max") else "min"
            value = extract_age(item.raw, prefer=prefer)
        else:
            prefer = "max" if item.field.endswith("_max") else "min"
            value = extract_integer(item.raw, prefer=prefer)
        if value is not None:
            item.resolved = str(value)
            item.method = "rule"
            item.confidence = "high"

    def _money(self, item: WorkItem, vocab: Vocabulary, collected_at: date | None) -> None:
        result = parse_money(item.raw)
        if result.negotiable:
            item.extra["negotiable"] = True
            item.method = "rule"
            item.resolved = None
            item.note = "negotiable"
            return
        if result.min_irr is None and result.amount is None:
            item.method = "rule"
            item.note = result.note or "no_amount"
            item.resolved = None
            return
        if result.currency != "IRR":
            item.extra.update({"currency": result.currency, "amount": result.amount})
        else:
            item.extra.update(
                {
                    "currency": "IRR",
                    "min": result.min_irr,
                    "max": result.max_irr,
                }
            )
        item.resolved = "money"  # marker; real ints go in extra
        item.method = "rule"
        item.confidence = "high"
