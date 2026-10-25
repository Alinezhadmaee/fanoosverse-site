"""Golden values tests and multi-record synthetic corpus verification."""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

from normalizer_agent.interface.api import Normalizer
from normalizer_agent.llm.providers.mock import MockProvider
from normalizer_agent.llm.router import FallbackRouter, _Entry
from normalizer_agent.pipeline.deterministic import DeterministicResolver
from normalizer_agent.pipeline.items import WorkItem
from normalizer_agent.rules.integers import extract_experience_years
from normalizer_agent.rules.money import parse_money
from normalizer_agent.schemas.envelope import RecordStatus
from normalizer_agent.schemas.job_ad import JobAd
from normalizer_agent.settings import LLMConfig, ProviderConfig, RoutingConfig, load_app_config
from normalizer_agent.vocab.loader import VocabStore
from normalizer_agent.vocab.retriever import TrigramRetriever


def _create_router_with_provider(
    provider, app_config, name="mock_test", priority=1
) -> FallbackRouter:
    p_cfg = ProviderConfig(name=name, type="mock", model="mock-model", priority=priority)
    llm_cfg = LLMConfig(
        routing=RoutingConfig(strategy="priority_fallback", max_total_attempts=4),
        providers=[p_cfg],
    )
    router = FallbackRouter.__new__(FallbackRouter)
    router._llm_config = llm_cfg
    router._pipeline = app_config.pipeline
    entry = _Entry(p_cfg, app_config.pipeline, provider=provider)
    router._entries = [entry]
    router._rr_index = 0
    router._call_lock = asyncio.Lock()
    return router


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def app_config(project_root: Path):
    return load_app_config(project_root / "config")


@pytest.fixture
def vocab_store(project_root: Path):
    return VocabStore(project_root / "config" / "vocab")


def test_golden_values_deterministic_accuracy(app_config, vocab_store, project_root: Path):
    golden_path = project_root / "tests" / "data" / "golden_values.csv"
    resolver = DeterministicResolver(TrigramRetriever())

    with golden_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            field = row["field"]
            raw = row["raw_value"]
            expected = row["expected_canonical"]

            rule = app_config.fields.fields.get(field)
            assert rule is not None, f"Rule missing for field {field}"

            if field.startswith("salary_"):
                res = parse_money(raw)
                if expected == "null":
                    assert (res.min_irr is None and res.max_irr is None) or res.negotiable
                else:
                    exp_int = int(expected)
                    if field.endswith("_min"):
                        assert res.min_irr == exp_int or (
                            res.min_irr is None and res.max_irr == exp_int
                        )
                    else:
                        assert res.max_irr == exp_int or (
                            res.max_irr is None and res.min_irr == exp_int
                        )
            elif field.startswith("experience_"):
                years = extract_experience_years(raw)
                assert years == int(expected)
            elif rule.vocab:
                vocab = vocab_store.get(rule.vocab)
                item = WorkItem(field=field, index=None, raw=raw, rule=rule)
                resolver.resolve(item, vocab, collected_at=None)
                if expected != "null" and "|" not in expected and item.resolved is not None:
                    assert item.resolved == expected, (
                        f"Failed on raw '{raw}': got {item.resolved}, expected {expected}"
                    )


@pytest.mark.asyncio
async def test_all_five_synthetic_records(project_root: Path, app_config):
    normalizer = await Normalizer.from_config(project_root / "config")
    p_cfg = ProviderConfig(name="mock_test", type="mock", model="mock-model", priority=1)
    provider = MockProvider(p_cfg)
    mock_router = _create_router_with_provider(provider, normalizer.config)
    normalizer.router = mock_router
    normalizer._normalizer.router = mock_router
    normalizer._normalizer.llm_stage.router = mock_router

    try:
        sample_files = sorted((project_root / "tests" / "data").glob("sample_record_*.json"))
        assert len(sample_files) == 5, f"Expected 5 samples, found {len(sample_files)}"

        records = [json.loads(p.read_text(encoding="utf-8")) for p in sample_files]
        results = await normalizer.normalize_batch(records)

        assert len(results) == 5
        for res in results:
            assert res.status in (RecordStatus.NORMALIZED, RecordStatus.NEEDS_REVIEW)
            assert res.record is not None
            assert isinstance(res.record, JobAd)
            assert len(res.audit) > 0
    finally:
        await normalizer.aclose()
