"""End-to-end tests for the file-based handoff pipeline.

The test feeds three real records from the extractor's processed dataset
through the adapter and checks the contract consumed by the vectorizer:
flat records keep ``text`` and ``role_title`` at the top level, provenance is
unchanged, and every input record lands in exactly one output queue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from normalizer_agent.schemas.envelope import NormalizeResult, RecordStatus, ReviewItem
from normalizer_agent.schemas.job_ad import JobAd
from run_pipeline import build_parser, run


@pytest.fixture
def project_root() -> Path:
    """Return the normalizer project root."""
    return Path(__file__).resolve().parent.parent


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of objects."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _StubNormalizer:
    """Deterministic stand-in for the real pipeline in handoff tests.

    The first record normalizes, the second needs review, and the third is
    rejected. This keeps the test focused on file routing and the flat-record
    contract rather than on vocabulary coverage.
    """

    async def normalize_batch(
        self, records: list[tuple[str, dict[str, Any]]]
    ) -> list[NormalizeResult]:
        results: list[NormalizeResult] = []
        for index, (record_id, record) in enumerate(records):
            if index == 0:
                results.append(
                    NormalizeResult(
                        record_id=record_id,
                        status=RecordStatus.NORMALIZED,
                        record=JobAd.model_validate(record),
                    )
                )
            elif index == 1:
                results.append(
                    NormalizeResult(
                        record_id=record_id,
                        status=RecordStatus.NEEDS_REVIEW,
                        review_items=[
                            ReviewItem(
                                field="organization",
                                raw=str(record.get("organization") or ""),
                                reason="low_confidence",
                            )
                        ],
                    )
                )
            else:
                results.append(
                    NormalizeResult(
                        record_id=record_id,
                        status=RecordStatus.REJECTED,
                        error_codes=["SCHEMA_MISMATCH"],
                    )
                )
        return results

    async def aclose(self) -> None:
        """Match the Normalizer interface used by the adapter."""
        return None


@pytest.mark.asyncio
async def test_file_handoff_contract(project_root: Path, tmp_path: Path) -> None:
    """Run the adapter on real records and verify the vectorizer contract."""
    input_path = project_root / "tests" / "data" / "processed_sample.jsonl"
    output_dir = tmp_path / "out"
    source_records = _read_jsonl(input_path)
    assert len(source_records) == 3

    args = build_parser().parse_args(
        ["--input", str(input_path), "--output-dir", str(output_dir)]
    )
    exit_code = await run(args, normalizer=_StubNormalizer())  # type: ignore[arg-type]

    assert exit_code == 1

    envelopes = _read_jsonl(output_dir / "normalized.jsonl")
    flat_records = _read_jsonl(output_dir / "normalized_records.jsonl")
    review_records = _read_jsonl(output_dir / "review_queue.jsonl")
    dead_records = _read_jsonl(output_dir / "dead_letter.jsonl")

    # One envelope per input record, and every record is in exactly one queue.
    assert len(envelopes) == len(source_records)
    assert len(flat_records) == 1
    assert len(review_records) == 1
    assert len(dead_records) == 1
    assert len(flat_records) + len(review_records) + len(dead_records) == len(source_records)

    source_by_id = {record["document_id"]: record for record in source_records}
    row = flat_records[0]
    assert "record" not in row
    assert "audit" not in row
    assert row["text"]
    assert row["role_title"]
    source = source_by_id[row["document_id"]]
    assert row["text"] == source["text"]
    assert row["role_title"] == source["role_title"]
    assert row["content_hash"] == source["content_hash"]
    assert row["collector"] == source["collector"]

    # The three queues are disjoint.
    flat_ids = {row["document_id"]}
    review_ids = {item["record_id"] for item in review_records}
    dead_ids = {item["record_id"] for item in dead_records}
    assert flat_ids.isdisjoint(review_ids)
    assert flat_ids.isdisjoint(dead_ids)
    assert review_ids.isdisjoint(dead_ids)
