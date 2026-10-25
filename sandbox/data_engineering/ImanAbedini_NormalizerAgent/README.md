# Normalizer Agent — Iman Abedini

LLM-based normalizer agent for Iranian job-advertisement field values.

The pipeline is deterministic-first: Unicode unification, Persian/Arabic
character normalization, unit conversion, and vocabulary lookups run before
any model call. A provider-agnostic LLM fallback layer handles only values
that rules and vocabularies cannot resolve. The output is a strict `JobAd`
model wrapped in an envelope with an audit trail, human-review items, usage
statistics, and status codes.

**Status:** implementation complete. See `PROJECT_README.md` for architecture,
configuration, and integration details.

## Contents

- `src/normalizer_agent/` — pipeline, rules, vocabularies, LLM providers,
  schemas, cache, and interfaces
- `config/` — `llm.yaml`, `fields.yaml`, `pipeline.yaml`, and vocabulary CSVs
- `prompts/` — versioned prompt templates
- `tests/` — unit, golden-value, interface, and end-to-end tests
- `docs/` — architecture, integration, provider, schema, and assumptions
- `tools/` — vocabulary seed generator
- `run_pipeline.py` — file-based coordinator adapter and vectorizer handoff

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows

pip install -e ".[service,dev]"
cp .env.example .env             # fill in the keys you actually use

normalizer validate-config -c config
pytest -q
```

## File-based pipeline handoff

```bash
python run_pipeline.py --input processed.jsonl --output-dir out
```

This writes `normalized.jsonl` (full envelope), `normalized_records.jsonl`
(flat records for the vectorizer), `review_queue.jsonl`, and
`dead_letter.jsonl`.
