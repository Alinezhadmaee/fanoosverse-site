"""Human-in-the-loop review sinks.

The pipeline writes every flagged item through a :class:`ReviewSink`; the
default implementation appends JSONL. The real database sink arrives with the
Coordinator integration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from normalizer_agent.schemas.envelope import ReviewItem


class ReviewSink(Protocol):
    def write(self, record_id: str, item: ReviewItem) -> None: ...
    def flush(self) -> None: ...


class JsonlReviewSink:
    """Append-only JSONL file; one object per line, UTF-8."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record_id: str, item: ReviewItem) -> None:
        payload = {
            "record_id": record_id,
            "field": item.field,
            "index": item.index,
            "raw": item.raw,
            "proposed": item.proposed,
            "reason": item.reason,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def flush(self) -> None:
        return None


class NullReviewSink:
    """Drops review items (used when the caller consumes review from envelopes)."""

    def write(self, record_id: str, item: ReviewItem) -> None:
        return None

    def flush(self) -> None:
        return None


class MemoryReviewSink:
    """Collects review items in memory for inspection/tests."""

    def __init__(self) -> None:
        self.items: list[tuple[str, ReviewItem]] = []

    def write(self, record_id: str, item: ReviewItem) -> None:
        self.items.append((record_id, item))

    def flush(self) -> None:
        return None
