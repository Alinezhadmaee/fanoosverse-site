"""Controlled-vocabulary loading, indexing, and lookup (stage 3).

Vocabulary CSV format (UTF-8, comma-separated):
    value,category,variants(|-separated)
``value`` is the canonical form, ``category`` an optional tag, ``variants``
are pipe-separated alternative spellings (any language). The vocabulary
version is the SHA-256 of the file bytes, truncated to 12 hex chars; it flows
into the audit trail and the LLM cache key.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

from normalizer_agent.text.unify import unify_key

logger = logging.getLogger(__name__)


class VocabularyError(Exception):
    pass


@dataclass(slots=True)
class VocabEntry:
    value: str
    category: str | None = None
    variants: tuple[str, ...] = ()

    def display(self) -> str:
        return self.value


@dataclass(slots=True)
class Vocabulary:
    name: str
    version: str
    entries: list[VocabEntry]
    _exact: dict[str, VocabEntry] = field(default_factory=dict, init=False)
    _variant_owner: dict[str, tuple[VocabEntry, str]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for entry in self.entries:
            key = unify_key(entry.value)
            if key in self._exact and self._exact[key].value != entry.value:
                logger.warning(
                    "vocab '%s': duplicate canonical value key '%s' ('%s' vs '%s')",
                    self.name,
                    key,
                    self._exact[key].value,
                    entry.value,
                )
            self._exact[key] = entry
            for variant in entry.variants:
                vkey = unify_key(variant)
                if not vkey:
                    continue
                if vkey in self._exact:
                    continue  # canonical keys always win over variants
                self._variant_owner.setdefault(vkey, (entry, variant))

    def __len__(self) -> int:
        return len(self.entries)

    def resolve(self, raw: str) -> tuple[VocabEntry, str] | None:
        """Exact then variant lookup on the unified key.

        Returns ``(entry, matched_text)`` where ``matched_text`` is the
        canonical value on an exact hit or the variant string on a variant
        hit; ``None`` when unresolved.
        """

        key = unify_key(raw)
        if not key:
            return None
        exact = self._exact.get(key)
        if exact is not None:
            return exact, exact.value
        hit = self._variant_owner.get(key)
        if hit is not None:
            return hit[0], hit[1]
        return None

    def values(self) -> list[str]:
        return [entry.value for entry in self.entries]

    def entries_by_category(self, category: str) -> list[VocabEntry]:
        return [e for e in self.entries if (e.category or "") == category]


_EMPTY = Vocabulary(name="empty", version="empty", entries=[])


def parse_vocab_csv(content: bytes | str, name: str) -> Vocabulary:
    """Parse CSV content into a :class:`Vocabulary` (version from content hash)."""

    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    entries: list[VocabEntry] = []
    reader = csv.reader(io.StringIO(text))
    for row_no, row in enumerate(reader):
        if not row or all(not cell.strip() for cell in row):
            continue
        if row_no == 0 and row and row[0].strip().lower() == "value":
            continue  # header
        value = row[0].strip()
        if not value:
            continue
        category = row[1].strip() if len(row) > 1 and row[1].strip() else None
        variants: tuple[str, ...] = ()
        if len(row) > 2 and row[2].strip():
            variants = tuple(v.strip() for v in row[2].split("|") if v.strip())
        entries.append(VocabEntry(value=value, category=category, variants=variants))
    return Vocabulary(name=name, version=version, entries=entries)


def load_vocabulary(path: Path) -> Vocabulary:
    name = path.stem
    try:
        return parse_vocab_csv(path.read_bytes(), name)
    except OSError as exc:
        raise VocabularyError(f"cannot read vocabulary {path}: {exc}") from exc
    except csv.Error as exc:
        raise VocabularyError(f"bad CSV in {path}: {exc}") from exc


class VocabStore:
    """All vocabularies of a config directory, loaded once, addressed by name."""

    def __init__(self, vocab_dir: Path) -> None:
        self.dir = vocab_dir
        self._vocabs: dict[str, Vocabulary] = {}
        if vocab_dir.is_dir():
            for path in sorted(vocab_dir.glob("*.csv")):
                vocab = load_vocabulary(path)
                self._vocabs[vocab.name] = vocab
        self._versions: dict[str, str] = {
            name: vocab.version for name, vocab in self._vocabs.items()
        }

    def get(self, name: str) -> Vocabulary:
        """Vocabulary by name; an empty vocabulary when the file is absent."""

        return self._vocabs.get(
            name,
            Vocabulary(
                name=name,
                version="empty",
                entries=[],
            ),
        )

    def has(self, name: str) -> bool:
        return name in self._vocabs

    def versions(self) -> dict[str, str]:
        return dict(self._versions)
