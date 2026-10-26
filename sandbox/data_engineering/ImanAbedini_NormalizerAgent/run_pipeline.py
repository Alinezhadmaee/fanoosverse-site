"""File-based coordinator adapter for the Fanos normalizer.

This script is the smallest end-to-end integration between the extraction
pipeline and the vectorization pipeline:

    processed.jsonl -> Normalizer -> normalized.jsonl
                                    normalized_records.jsonl
                                    review_queue.jsonl
                                    dead_letter.jsonl

The flat ``normalized_records.jsonl`` is the handoff contract for
``gemma_vectorizer.py``: it keeps ``text`` and ``role_title`` at the top level
and stores every other canonical field as Qdrant payload.

Usage:
    python run_pipeline.py --input processed.jsonl --output-dir out
    python run_pipeline.py --input processed.jsonl --output-dir out --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from normalizer_agent.interface.api import Normalizer
from normalizer_agent.schemas.envelope import NormalizeResult, RecordStatus

logger = logging.getLogger("fanos.normalizer.pipeline")

BATCH_SIZE = 50


def read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """Yield ``(line_number, record, error)`` for every non-empty JSONL line."""
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                yield line_number, None, f"invalid JSON: {exc}"
                continue
            if not isinstance(record, dict):
                yield line_number, None, "record must be a JSON object"
                continue
            yield line_number, record, None


def write_jsonl(handle: Any, payload: dict[str, Any]) -> None:
    """Write one compact JSON object followed by a newline."""
    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def flat_record(result: NormalizeResult) -> dict[str, Any]:
    """Return the canonical record in the shape expected by the vectorizer."""
    if result.record is None:
        raise ValueError(f"record {result.record_id} has no canonical JobAd")
    return result.record.model_dump(mode="json")


def chunked(
    items: Iterable[tuple[int, dict[str, Any] | None, str | None]], size: int
) -> Iterator[list[tuple[int, dict[str, Any] | None, str | None]]]:
    """Group input records into batches while preserving line numbers."""
    batch: list[tuple[int, dict[str, Any] | None, str | None]] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _invalid_envelope(line_number: int, error: str) -> dict[str, Any]:
    """Build a dead-letter envelope for an input line that is not valid JSON."""
    return {
        "record_id": f"line_{line_number}",
        "status": "failed",
        "error_codes": ["INVALID_INPUT"],
        "error": error,
    }


async def run(args: argparse.Namespace, *, normalizer: Normalizer | None = None) -> int:
    """Normalize an input file and write the four handoff outputs.

    ``normalizer`` is injectable so tests can supply a mock-backed instance
    without changing the CLI behavior.
    """
    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"input file not found: {input_path}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    owns_normalizer = normalizer is None
    if normalizer is None:
        normalizer = await Normalizer.from_config(args.config)

    counts = {
        "records": 0,
        "normalized": 0,
        "needs_review": 0,
        "failed": 0,
        "rejected": 0,
    }

    normalized_path = output_dir / "normalized.jsonl"
    records_path = output_dir / "normalized_records.jsonl"
    review_path = output_dir / "review_queue.jsonl"
    dead_path = output_dir / "dead_letter.jsonl"

    try:
        with (
            normalized_path.open("w", encoding="utf-8") as normalized_file,
            records_path.open("w", encoding="utf-8") as records_file,
            review_path.open("w", encoding="utf-8") as review_file,
            dead_path.open("w", encoding="utf-8") as dead_file,
        ):
            consumed = 0
            for batch in chunked(read_jsonl(input_path), BATCH_SIZE):
                if args.limit is not None and consumed >= args.limit:
                    break
                if args.limit is not None:
                    batch = batch[: args.limit - consumed]

                records: list[tuple[str, dict[str, Any]]] = []
                for line_number, record, error in batch:
                    counts["records"] += 1
                    if error is not None or record is None:
                        counts["failed"] += 1
                        envelope = _invalid_envelope(line_number, error or "unknown error")
                        write_jsonl(normalized_file, envelope)
                        write_jsonl(dead_file, envelope)
                        continue
                    record_id = str(record.get("document_id") or f"line_{line_number}")
                    records.append((record_id, record))

                consumed += len(batch)
                if records:
                    results = await normalizer.normalize_batch(records)

                    for (_record_id, _record), result in zip(records, results, strict=True):
                        envelope = result.model_dump(mode="json")
                        write_jsonl(normalized_file, envelope)

                        if result.status is RecordStatus.NORMALIZED:
                            counts["normalized"] += 1
                            write_jsonl(records_file, flat_record(result))
                        elif result.status is RecordStatus.NEEDS_REVIEW:
                            counts["needs_review"] += 1
                            write_jsonl(review_file, envelope)
                        elif result.status is RecordStatus.REJECTED:
                            counts["rejected"] += 1
                            write_jsonl(dead_file, envelope)
                        else:
                            counts["failed"] += 1
                            write_jsonl(dead_file, envelope)

                print(f"processed {consumed} records")
    finally:
        if owns_normalizer:
            await normalizer.aclose()

    print("summary:", json.dumps(counts))
    print("outputs:", output_dir)
    return 0 if counts["failed"] == 0 and counts["rejected"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize a JSONL file for the Fanos pipeline.")
    parser.add_argument("--input", required=True, help="input JSONL file (processed.jsonl)")
    parser.add_argument("--output-dir", required=True, help="directory for output files")
    parser.add_argument("--config", default="config", help="normalizer config directory")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum number of records to process",
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
