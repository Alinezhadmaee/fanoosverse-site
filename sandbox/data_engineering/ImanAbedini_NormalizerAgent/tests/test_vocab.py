"""Tests for vocab/: loading, exact/variant lookup, versioning, retrieval."""

from __future__ import annotations

from pathlib import Path

from normalizer_agent.vocab.loader import VocabEntry, Vocabulary, parse_vocab_csv
from normalizer_agent.vocab.retriever import TrigramRetriever


def _vocab() -> Vocabulary:
    return parse_vocab_csv(
        "value,category,variants\n"
        'Python,programming_language,"پایتون|py|python3"\n'
        'Machine Learning,concept,"یادگیری ماشین"\n'
        'LLM,concept,""\n',
        "capability",
    )


class TestLoader:
    def test_parse_and_hash(self) -> None:
        vocab = _vocab()
        assert len(vocab) == 3
        assert vocab.version and len(vocab.version) == 12
        assert vocab.entries[0].category == "programming_language"

    def test_version_changes_with_content(self) -> None:
        a = parse_vocab_csv("value,category,variants\nA,,\n", "x")
        b = parse_vocab_csv("value,category,variants\nA,,\nB,,\n", "x")
        assert a.version != b.version

    def test_duplicate_values_collide_without_crash(self) -> None:
        vocab = parse_vocab_csv("value,category,variants\nA,,\nA,,\n", "x")
        assert len(vocab) == 2
        assert vocab.resolve("A") is not None


class TestLookup:
    def test_exact(self) -> None:
        result = _vocab().resolve("python")
        assert result is not None and result[0].value == "Python"

    def test_variant(self) -> None:
        result = _vocab().resolve("پایتون")
        assert result is not None
        entry, matched = result
        assert entry.value == "Python" and matched == "پایتون"

    def test_variant_unified(self) -> None:
        # case-folding and digit/letter unification apply to variants too
        result = _vocab().resolve("Python3")
        assert result is not None and result[0].value == "Python"

    def test_miss(self) -> None:
        assert _vocab().resolve("haskell") is None

    def test_empty_key(self) -> None:
        assert _vocab().resolve("   ") is None

    def test_canonical_wins_over_variant(self) -> None:
        # a value that is also another entry's variant resolves to the canonical
        vocab = parse_vocab_csv("value,category,variants\nB,,\nA,,B\n", "x")
        assert vocab.resolve("B")[0].value == "B"


class TestStore:
    def test_empty_store_serves_empty_vocab(self) -> None:
        from normalizer_agent.vocab.loader import VocabStore

        store = VocabStore(Path("nonexistent-dir"))
        vocab = store.get("capability")
        assert len(vocab) == 0 and vocab.version == "empty"

    def test_real_seed_vocab_loads(self) -> None:
        from normalizer_agent.vocab.loader import VocabStore

        vocab_dir = Path(__file__).resolve().parents[1] / "config" / "vocab"
        store = VocabStore(vocab_dir)
        cap = store.get("capability")
        assert len(cap) > 50
        assert store.get("capability").resolve("پایتون")[0].value == "Python"
        assert "capability" in store.versions()
        emp = store.get("employment_type")
        assert emp.resolve("تمام‌وقت")[0].value == "full_time"
        assert emp.resolve("پاره وقت")[0].value == "part_time"


class TestRetriever:
    def test_ranks_closest_first(self) -> None:
        vocab = _vocab()
        retriever = TrigramRetriever()
        candidates = retriever.retrieve(vocab, "pythonn", 3)
        assert candidates[0][0].value == "Python"

    def test_variant_match_scores(self) -> None:
        vocab = _vocab()
        candidates = TrigramRetriever().retrieve(vocab, "یادگیری ماشن", 3)
        assert candidates[0][0].value == "Machine Learning"

    def test_top_k(self) -> None:
        vocab = _vocab()
        assert len(TrigramRetriever(min_score=0.0).retrieve(vocab, "zzz", 2)) <= 2

    def test_entry_dataclass(self) -> None:
        entry = VocabEntry(value="X", category=None, variants=())
        assert entry.display() == "X"
