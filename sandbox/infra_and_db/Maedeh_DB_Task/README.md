# Job-ad storage and query layer: proof of concept

Proof of concept for storing, validating and querying scraped job ads.
The full reasoning and all measured results are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Contents

| File | Purpose |
|---|---|
| `ARCHITECTURE.md` | Architecture document: database choice, validation design, benchmark results |
| `gen_mock.py` | Generates mock raw and processed job-ad files with labeled defects |
| `validate.py` | Four-layer validation (JSON, schema, record, batch) with pydantic and jsonschema engines |
| `pg_poc.py` | Compares jsonb, typed and hybrid storage layouts in PostgreSQL |
| `lite_duck_poc.py` | Early proof of concept on SQLite and DuckDB |
| `bench8.py` | Loads synthetic data into SQLite, DuckDB and PostgreSQL and benchmarks queries Q1-Q7 |
| `bench_out/`, `bench_out_300k/` | Benchmark results for 100k and 300k rows |

## Quick start

```
pip install -r requirements.txt
python gen_mock.py --n 10000 --seed 42 --prefix mock
python validate.py --raw mock_raw.jsonl --processed mock_processed.jsonl --labels mock_labels.jsonl --engine both
python bench8.py --n 100000
```

The benchmark needs a local PostgreSQL server and the `PGPASSWORD` environment variable.
See section 10 of `ARCHITECTURE.md` for the full steps.
