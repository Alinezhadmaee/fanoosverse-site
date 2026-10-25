"""Persistent LLM result cache (SQLite).

Key: ``(field_kind, normalized_raw_value, vocab_version, prompt_version,
llm_mode)`` — the model is stored for the audit trail but is NOT part of the
key by default (``key_includes_model: false``): the same task may be answered
by any acceptable model. A cache hit skips the LLM entirely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from normalizer_agent import PROMPT_VERSION


@dataclass(slots=True)
class CacheEntry:
    canonical: list[str] | None
    category: str | None
    confidence: Literal["high", "low"]
    reason: str
    provider: str
    model: str


def cache_key(
    *,
    field_kind: str,
    raw_value: str,
    vocab_version: str,
    llm_mode: str,
    model: str = "",
    key_includes_model: bool = False,
) -> str:
    material = "|".join(
        [
            field_kind,
            raw_value.strip().casefold(),
            vocab_version,
            PROMPT_VERSION,
            llm_mode,
            model if key_includes_model else "",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class LLMCache:
    def __init__(self, path: str | Path, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        if not enabled:
            self._conn: sqlite3.Connection | None = None
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file explicitly. Some sandboxed Windows environments deny
        # SQLite's O_CREAT path even when the directory itself is writable;
        # touching first also gives a clear error if the path is not writable.
        path.touch(exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS answers (
                key TEXT PRIMARY KEY,
                answer TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.commit()

    def get(self, key: str) -> CacheEntry | None:
        if not self.enabled or self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT answer, provider, model FROM answers WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            self._misses += 1
            return None
        self._hits += 1
        answer = json.loads(row[0])
        confidence_raw = answer.get("confidence", "low")
        return CacheEntry(
            canonical=answer.get("canonical"),
            category=answer.get("category"),
            confidence=confidence_raw if confidence_raw in {"high", "low"} else "low",
            reason=answer.get("reason", "cache"),
            provider=row[1] or "cache",
            model=row[2] or "",
        )

    def put(
        self,
        key: str,
        *,
        canonical: list[str] | None,
        category: str | None,
        confidence: Literal["high", "low"],
        reason: str,
        provider: str,
        model: str,
    ) -> None:
        if not self.enabled or self._conn is None:
            return
        answer = json.dumps(
            {
                "canonical": canonical,
                "category": category,
                "confidence": confidence,
                "reason": reason,
            },
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO answers (key, answer, provider, model) VALUES (?, ?, ?, ?)",
                (key, answer, provider, model),
            )
            self._conn.commit()

    @property
    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
