"""Structured JSON logging: record_id, stage, provider, latency.

No secrets and no full ad text at INFO level (task 2.7 / 8): text fields are
truncated and the ad body is never logged.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_REDACTED_KEYS = {"api_key", "authorization", "text", "content_hash"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_data", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key.lower() in _REDACTED_KEYS:
                    payload[key] = "<redacted>"
                elif isinstance(value, str) and len(value) > 200:
                    payload[key] = value[:200] + "…"
                else:
                    payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("normalizer_agent")
    root.handlers = [handler]
    root.setLevel(level.upper())
    root.propagate = False


def log(logger: logging.Logger, level: int, message: str, **data: Any) -> None:
    logger.log(level, message, extra={"extra_data": data})


class Metrics:
    """In-process counters; printed once per run via ``as_dict``."""

    def __init__(self) -> None:
        self.records_in = 0
        self.records_normalized = 0
        self.records_needs_review = 0
        self.records_failed = 0
        self.records_rejected = 0
        self.items_rule = 0
        self.items_lookup = 0
        self.items_llm = 0
        self.items_cache = 0
        self.items_review = 0
        self.llm_input_tokens = 0
        self.llm_output_tokens = 0
        self.llm_cost_usd = 0.0
        self.llm_calls = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return f"Metrics({self.as_dict()!r})"
