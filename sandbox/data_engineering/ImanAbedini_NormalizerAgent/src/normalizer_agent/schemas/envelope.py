"""Output envelope models shared by the Python API, CLI, and HTTP service."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from normalizer_agent.schemas.job_ad import JobAd


class RecordStatus(StrEnum):
    NORMALIZED = "normalized"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    REJECTED = "rejected"


class AuditEntry(BaseModel):
    """One normalized value: what went in, what came out, and how."""

    model_config = ConfigDict(extra="forbid")

    field: str
    index: int | None = None
    raw: str
    canonical: str | None = None
    method: Literal["rule", "lookup", "llm", "cache", "none"] = "rule"
    confidence: Literal["high", "low"] | None = None
    provider: str | None = None
    model: str | None = None
    vocab_version: str | None = None
    note: str | None = None


ReviewReason = Literal[
    "not_in_vocabulary",
    "low_confidence",
    "inferred",
    "validation_failed",
]


class ReviewItem(BaseModel):
    """An item a human must look at before it can enter the database."""

    model_config = ConfigDict(extra="forbid")

    field: str
    index: int | None = None
    raw: str
    proposed: str | None = None
    reason: ReviewReason


class UsageStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    llm_calls: int = 0

    def add(self, other: UsageStats) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.estimated_cost_usd += other.estimated_cost_usd
        self.llm_calls += other.llm_calls


class NormalizeRequest(BaseModel):
    """Coordinator -> Normalizer request contract (docs/INTEGRATION.md)."""

    model_config = ConfigDict(extra="allow")

    record_id: str | None = None
    record: dict[str, object]
    trace_id: str | None = None


class NormalizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    status: RecordStatus
    record: JobAd | None = None
    error_codes: list[str] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)
    audit: list[AuditEntry] = Field(default_factory=list)
    usage: UsageStats = UsageStats()
    versions: dict[str, str | dict[str, str]] = {}
    trace_id: str | None = None
