"""Work items: one raw value travelling through the pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal

from normalizer_agent.settings import FieldRule

Method = Literal["rule", "lookup", "llm", "cache", "none"]
Confidence = Literal["high", "low"]
ReviewReasonName = Literal[
    "not_in_vocabulary",
    "low_confidence",
    "inferred",
    "validation_failed",
]


@dataclass(slots=True)
class WorkItem:
    """One raw (sub)value to normalize; list position tracked via ``index``."""

    field: str
    index: int | str | None
    raw: str
    rule: FieldRule
    # state
    resolved: str | None = None  # canonical value (or None if unresolved)
    extra: dict[str, Any] = dataclass_field(default_factory=dict)  # kind-specific output
    method: Method = "rule"  # rule | lookup | llm | cache | none
    confidence: Confidence | None = None
    provider: str | None = None
    model: str | None = None
    note: str | None = None
    removed: list[str] = dataclass_field(default_factory=list)  # parentheticals etc.
    review_reason: ReviewReasonName | None = None
    candidate_new: bool = False

    @property
    def is_place(self) -> bool:
        return self.rule.kind == "location"


@dataclass(slots=True)
class RawFieldValues:
    """Raw values of one field after type coercion."""

    field: str
    scalar: bool
    values: list[tuple[int | None, str]]  # (index, raw)
