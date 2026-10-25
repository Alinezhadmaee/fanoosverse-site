# Fanos Normalizer Agent — Integration Guide

This guide shows how to use the normalizer as a Python library, how to wire
it into the Fanos coordinator, and how to run it as a REST service or CLI.

---

## 1. Python in-process API

```python
import asyncio

from normalizer_agent.interface.api import Normalizer


async def run() -> None:
    normalizer = await Normalizer.from_config("config")
    try:
        raw_record = {
            "document_id": "job_jobinja_0001",
            "collector": "jobinja",
            "source_type": "job_posting",
            "role_title": "Python Developer",
            "skills_required": ["Python", "Django", "Docker"],
            "salary_min": "40 million toman",
            "salary_max": "60 million toman",
            "text": "Full job-ad text ...",
            "content_hash": "a1b2c3d4e5f6...",
            "collected_at": "2026-08-30",
        }

        result = await normalizer.normalize(raw_record)
        print("status:", result.status)
        if result.record is not None:
            print("skills:", result.record.skills_required)
            print("salary_min (IRR):", result.record.salary_min)
    finally:
        await normalizer.aclose()


asyncio.run(run())
```

`NormalizeRequest` in `schemas/envelope.py` is the coordinator-to-normalizer
contract: `record_id`, `record`, and optional `trace_id`.

---

## 2. Coordinator integration

`AbstractCoordinatorAdapter` in `interface/coordinator.py` defines the
contract for Celery, Kafka, RabbitMQ, Temporal, or any other queue. The
adapter deals with three outcomes:

- `normalized` → canonical storage queue
- `needs_review` → human review queue
- `rejected` / `failed` → dead-letter queue

`CoordinatorMessage` is a `Protocol`, not a concrete class; do not
instantiate it. Wrap your queue message in a small dataclass:

```python
from dataclasses import dataclass

from normalizer_agent.interface.coordinator import ReferenceCoordinatorAdapter


@dataclass
class QueueMessage:
    id: str
    body: dict
    trace_id: str | None = None


adapter = ReferenceCoordinatorAdapter(normalizer)
message = QueueMessage(
    id="msg_001",
    body=raw_record,
    trace_id="trace_abc_123",
)
result = await adapter.process_item(message)
```

`ReferenceCoordinatorAdapter` is an in-memory reference implementation. For
production, implement `process_item`, `process_batch`, and `handle_result`
against the real queue and database.

### File-based handoff

For the current Fanos sandbox pipeline, a file-based adapter is enough:
`run_pipeline.py` reads `processed.jsonl` from the extractor, normalizes each
line, and writes:

```bash
python run_pipeline.py --input processed.jsonl --output-dir out
```

| Output | Content |
|---|---|
| `normalized.jsonl` | full `NormalizeResult` envelope, for audit |
| `normalized_records.jsonl` | flat `JobAd` records, for the vectorizer |
| `review_queue.jsonl` | records with `status == needs_review` |
| `dead_letter.jsonl` | records with `status` in `rejected` / `failed` |

The flat record must keep `text` and `role_title` at the top level, because
`gemma_vectorizer.py` reads those keys directly and copies every other key
into the Qdrant payload.

---

## 3. REST service

The optional FastAPI service lives in `interface/service.py`:

```bash
uvicorn normalizer_agent.interface.service:app --host 0.0.0.0 --port 8000
```

or, using the factory explicitly:

```bash
uvicorn normalizer_agent.interface.service:create_app --factory --port 8000
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness, version, and enabled providers |
| `GET /metrics` | Pipeline counters, tokens, and estimated cost |
| `POST /normalize` | Normalize one record |
| `POST /normalize/batch` | Normalize a list of records |

The service reads its config directory from `NORMALIZER_CONFIG_DIR` or from
the default `config/` path.

---

## 4. CLI

```bash
# Validate llm.yaml, fields.yaml, pipeline.yaml, and schema consistency
normalizer validate-config -c config

# Probe every enabled provider with a live request
normalizer llm-check -c config

# Normalize a JSON object, JSON array, or JSONL file
normalizer run -i raw_jobs.jsonl -o normalized_jobs.jsonl -c config
```

Every input record produces exactly one output envelope. A malformed input
line produces a `failed` envelope instead of stopping the run.
