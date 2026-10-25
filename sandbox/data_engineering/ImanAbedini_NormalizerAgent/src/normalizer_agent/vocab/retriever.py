"""Candidate retrieval for ``candidates`` LLM mode.

:class:`CandidateRetriever` is the interface the pipeline depends on; the
default implementation ranks vocabulary entries by character-trigram
similarity (no external dependency). An embedding-based implementation can be
added later without touching the pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from normalizer_agent.text.unify import unify_key
from normalizer_agent.vocab.loader import VocabEntry, Vocabulary


class CandidateRetriever(Protocol):
    def retrieve(
        self, vocab: Vocabulary, raw_value: str, top_k: int
    ) -> list[tuple[VocabEntry, float]]: ...


@dataclass(slots=True)
class TrigramRetriever:
    """Rank candidates by Jaccard similarity of character trigrams."""

    min_score: float = 0.05

    @staticmethod
    def _trigrams(key: str) -> set[str]:
        padded = f"  {key} "
        return {padded[i : i + 3] for i in range(max(1, len(padded) - 2))}

    def score(self, a: str, b: str) -> float:
        ka, kb = unify_key(a), unify_key(b)
        if not ka or not kb:
            return 0.0
        ga, gb = self._trigrams(ka), self._trigrams(kb)
        if not ga or not gb:
            return 0.0
        return len(ga & gb) / len(ga | gb)

    def retrieve(
        self, vocab: Vocabulary, raw_value: str, top_k: int
    ) -> list[tuple[VocabEntry, float]]:
        scored: list[tuple[VocabEntry, float]] = []
        query = unify_key(raw_value)
        for entry in vocab.entries:
            best = self.score(query, unify_key(entry.value))
            for variant in entry.variants:
                best = max(best, self.score(query, unify_key(variant)))
            if best >= self.min_score:
                scored.append((entry, best))
        scored.sort(key=lambda pair: (-pair[1], pair[0].value))
        return scored[:top_k]


def candidate_values(scored: Iterable[tuple[VocabEntry, float]]) -> list[str]:
    return [entry.value for entry, _ in scored]


def as_prompt_list(values: Sequence[str]) -> str:
    return ", ".join(values)
