# Architecture: job-ad storage and query layer (Task 2)

Status: proposal based on measured results, 2026-10-29.

## Summary of decisions

| Question | Decision | Main reason |
|---|---|---|
| Primary database | PostgreSQL, hybrid layout (typed columns for filtered and aggregated fields, `jsonb` for the rest) | Best all-round: fastest lookups, filters and skill joins, constraints, flexible remainder (sections 3, 4) |
| Text search | `pg_trgm` GIN index on the description column | Cuts substring search from 487 ms to 37 ms at 300k rows (section 4) |
| Analytics | DuckDB as an optional read-only sidecar | 18 to 30 times faster than PostgreSQL on aggregates, about a quarter of the disk space (section 4) |
| Local development and tests | SQLite | Simple, fast on point lookups, slow on scans and aggregates (section 4) |
| Validation | Four layers (JSON, schema, record, batch) before load; pydantic as default engine, jsonschema as cross-check; reject, do not repair | Cross-field errors need more than a schema check; pydantic is 3 to 20 times faster with identical detection (section 6) |

## 1. Purpose and scope

The brief asks for four things: high-volume mock data, validation, queries with a benchmark, and one architecture document. This document covers the last one and links the other three.

| Brief item | Deliverable |
|---|---|
| High-volume mock data | `gen_mock.py` (10,000-record raw and processed files with labeled defects) and `bench8.py` (synthetic jobs and skills, 100k to 300k rows) |
| Validation | `validate.py`; on 10,000-record mock files it rejected 1,207 raw (12.1%) and 1,187 processed (11.9%) records |
| Queries with benchmark | Q1-Q7 in `bench8.py`; results in `bench_out/BENCHMARK.md` and `bench_out_300k/BENCHMARK.md` |
| Storage layout comparison | `pg_poc.py` (jsonb vs typed vs hybrid, section 3) |
| Architecture document | this file |

## 2. Data flow

1. Generate or collect job ads (mock data, or real data with `--skip-hash`).
2. Validate every record; rejected records are counted and never loaded.
3. Load valid records into the store.
4. Serve the query set (Q1-Q7) and re-run the benchmark after any schema or index change.

Every benchmark answer is compared with an independent pure-Python oracle, so a fast but wrong result is reported as a mismatch. All result sets in every run matched the oracle.

## 3. Storage layout (about 8.8k records, PostgreSQL, `pg_poc.py`)

Three layouts were compared: all fields in one `jsonb` column, fully typed columns, and a hybrid (typed columns for filtered and aggregated fields, `jsonb` for the rest).

| Layout | Total size (MB) | Q4 (ms) | Q6 (ms) |
|---|---:|---:|---:|
| jsonb | 45.6 | 419 | 246 |
| typed | 39.9 | 6.6 | 6.3 |
| hybrid | 44.1 | 2.9 | 4.3 |

The `jsonb`-only layout is about 40 to 140 times slower than the other two on these queries. The hybrid layout had the best query times on both queries. It is about 10% larger than the typed layout (44.1 vs 39.9 MB) and close to the `jsonb` size (45.6 MB), and it keeps unqueried fields flexible. Proposal: use the hybrid layout.

## 4. Engine comparison (synthetic data, one machine, `bench8.py`)

Setup: Windows, Python 3.14, PostgreSQL 18.6, SQLite and DuckDB in-process. Median of 7 runs after one warm-up. PostgreSQL ran `VACUUM (ANALYZE)` after load and after the trigram index.

Queries: Q1 lookup by id; Q2 filter on four columns; Q3 top 20 jobs for one skill (join); Q4 count in a date range; Q5 top 20 skills (group by); Q6 salary stats by seniority; Q7 substring search in raw text (about 3% of rows match).

### 100,000 jobs (median ms)

| Query | SQLite | DuckDB | PostgreSQL |
|---|---:|---:|---:|
| Q1 by id | 0.07 | 0.86 | 0.16 |
| Q2 filter | 0.17 | 6.13 | 0.36 |
| Q3 skill top 20 | 67.47 | 17.16 | 3.19 |
| Q4 count range | 1.25 | 1.15 | 1.82 |
| Q5 top skills | 66.91 | 4.79 | 128.87 |
| Q6 salary stats | 119.15 | 3.68 | 94.50 |
| Q7 full scan | 110.25 | 57.21 | 299.53 |
| Q7 with trigram index | - | - | 8.59 |

### 300,000 jobs (median ms)

| Query | SQLite | DuckDB | PostgreSQL |
|---|---:|---:|---:|
| Q1 by id | 0.10 | 0.98 | 0.17 |
| Q2 filter | 0.84 | 8.57 | 0.51 |
| Q3 skill top 20 | 258.50 | 43.76 | 7.29 |
| Q4 count range | 4.29 | 2.32 | 4.89 |
| Q5 top skills | 342.30 | 8.83 | 265.03 |
| Q6 salary stats | 474.91 | 7.84 | 142.11 |
| Q7 full scan | 375.78 | 123.65 | 486.90 |
| Q7 with trigram index | - | - | 36.73 |

### Size and build cost

| Engine | Size 100k (MB) | Size 300k (MB) | Load + index 100k (s) | Load + index 300k (s) |
|---|---:|---:|---:|---:|
| SQLite | 84.9 | 255.2 | 1.86 + 1.53 | 7.49 + 10.14 |
| DuckDB | 23.3 | 68.5 | 1.10 + 0.11 | 3.01 + 0.32 |
| PostgreSQL | 103.5 | 306.3 | 1.62 + 5.86 | 5.79 + 19.69 |

The PostgreSQL trigram index adds about 76 MB at 100k and about 218 MB at 300k, measured on the first run of each size.

### Findings

- Point and filter lookups (Q1, Q2, Q3): row stores with indexes win. PostgreSQL is best on the join in Q3 (6x faster than DuckDB and 35x faster than SQLite at 300k).
- Aggregations (Q5, Q6): DuckDB is 18 to 30 times faster than PostgreSQL and uses about a quarter of the disk space of PostgreSQL (without its trigram index).
- Text search (Q7): a full scan is slow in every engine. The PostgreSQL trigram index cuts it from 487 ms to 37 ms at 300k (13x) and from 300 ms to 8.6 ms at 100k (35x), at the cost of a large index.
- The speedup from the trigram index depends on how rare the search term is. In this data about 3% of rows match, so most table pages still need a recheck. A rarer term should gain more; this was not measured.

## 5. Database types considered

Scraped job ads are raw and uneven: fields differ by source and change over time, records arrive duplicated or partial, and the original payload may need to be kept. Queries are filters, joins to skills, aggregates and text search over standardized fields.

| Option | Type | Evidence | Fit for this workload |
|---|---|---|---|
| PostgreSQL (typed columns + jsonb) | Relational with a document column | Measured (sections 3 and 4) | Best all-round: typed columns for standardized fields, jsonb for the variable remainder, constraints, trigram text search. Slower than columnar engines on large aggregates. |
| DuckDB | Embedded columnar | Measured (section 4) | Best on aggregates, small files, no server; in-process only. |
| SQLite | Embedded row store | Measured (section 4) | Simple and fast on point lookups; slow on scans and aggregates at 300k rows. |
| MongoDB | Document store | Not benchmarked | Natural for varying raw documents and offers optional JSON Schema validation; joins and group-bys need aggregation pipelines, and its text indexes are word based, not substring. Needs a separate server install. |
| Elasticsearch / OpenSearch | Search engine | Not benchmarked | Strong for ranked full-text search; an extra system to run and keep in sync, so better as an index beside the primary store than as the primary. |
| Object storage with JSONL or Parquet files | Raw archive | Not benchmarked | Cheap append-only landing zone for replay; no query layer. |
| Vector database (for example Qdrant) | Vector index | Out of scope here | Needed only for embedding search (Task 1), not for these queries. |

Rows marked "Not benchmarked" are qualitative judgments, not results.

## 6. Validation layers

Scraped data cannot be trusted at the door, so every record passes four layers before it is loaded. Rules for the raw layer (4 fields) and the processed layer (41 fields) live in one declarative table in `validate.py`, so changing the engine never changes the rules.

| Layer | Checks | Implementation |
|---|---|---|
| L0 | Line is valid JSON | stdlib |
| L1 schema | All fields present, no extra fields, strict types (no coercion), enum values, non-negative numbers, date format, non-blank strings, id pattern | pydantic or jsonschema (switchable) |
| L2 record | Id matches collector; min <= max for salary, age and company size; SHA-256 of text equals `content_hash` (off with `--skip-hash` on real data, where the hash recipe is unknown) | stdlib |
| L3 batch | Duplicate ids; raw to processed reconcile (information only) | stdlib |

Each failed record gets a level and a short message (field and error type for L1). A record that fails any layer is not loaded.

### Measured on mock data (10,000 records per layer, defects injected and labeled)

| Metric | Raw layer | Processed layer |
|---|---:|---:|
| Records rejected | 1,207 (12.1%) | 1,187 (11.9%) |
| Caught by L1 / L2 / L3 | 1,027 / 0 / 180 | 847 / 239 / 101 |
| Precision (labeled defects) | 100% | 100% |
| Recall (labeled defects) | 97.8% | 98.3% |
| L1 time per record, pydantic | 10 us | 21 us |
| L1 time per record, jsonschema | 33 us | 430 us |

Both engines produced identical failure counts and identical per-defect detection. Extrapolated to 1,000,000 processed records on one thread, L1 would take about 21 s with pydantic and about 430 s with jsonschema.

### Measured on real data (hash check off)

| Metric | Raw layer | Processed layer |
|---|---:|---:|
| Records | 204 | 203 |
| Rejected (both engines) | 0 | 0 |
| L1 time per record, pydantic | 8 us | 26 us |
| L1 time per record, jsonschema | 35 us | 458 us |

The strict 41-field schema accepted every real processed record. Reconcile found one id only in the raw file and no text mismatches. The L1 timing ratios are similar to those on mock data (about 4x on raw and about 18x on processed, against about 3x and about 20x on mock data). The SHA-256 check was off (`--skip-hash`) in this run. A separate run with it on rejected all 203 processed records as `hash_mismatch` (all passed L1), so plain SHA-256 of the UTF-8 `text` field does not reproduce the real `content_hash`.

Open question: how the real `content_hash` is computed. Until the data provider confirms the recipe, the hash check stays off for real data; once it is known, the check can be re-enabled by changing the hash function in `validate.py`.

### Why this design

- **Layered**: a schema check alone cannot see cross-field errors. On the processed layer, 239 records (2.4%) passed L1 and were still wrong (hash mismatch 118, inverted salary range 121). L3 caught another 101 duplicates.
- **Strict schema**: no coercion and no unknown fields, so renamed or extra fields and wrong types from a changed source page are rejected instead of silently stored (221 extra-field defects were caught on the raw layer).
- **pydantic as default L1 engine**: same detection as jsonschema, 3x faster on the raw layer and 20x faster on the processed layer, with typed errors that include the field path.
- **jsonschema kept as a cross-check**: the same rules are also available as standard JSON Schema, which other tools can read, and agreement between two independent engines is evidence that the rules are expressed correctly.
- **Reject, do not repair**: rejected records are counted and reported; the clean tables only hold records that passed every layer.

### Other approaches considered (not benchmarked)

| Approach | Note |
|---|---|
| Great Expectations, Pandera | Built for table or dataframe expectations and quality reports; a good fit for batch profiling, heavier than needed for per-record rejection before load. |
| Hand-written checks only | Simple, but every rule and error message is custom code; the declarative table plus an engine keeps rules in one place. |
| Database constraints (NOT NULL, CHECK, UNIQUE) | Kept as the last line of defense in PostgreSQL, but one bad row can abort a bulk load, so they are not a replacement for validation before load. |

### Known behavior to document

- Missed duplicates: 27 raw and 21 processed labeled duplicates were not flagged. As written, `run()` registers an id as seen only after a record passes L1 and L2, so a copy whose earlier twin was rejected is accepted as the only valid record with that id. This keeps one clean record per id; it was not verified record by record.
- Reconcile on mock data: raw 9,800 ids, processed 9,864 ids, 134 only in raw, 198 only in processed, 723 with different text. This is information only and was not investigated.

## 7. Proposed architecture

- **Validation before load: `validate.py`**, four layers, pydantic as default L1 engine, jsonschema as cross-check (section 6).
- **Primary store: PostgreSQL**, hybrid layout. It serves lookups, filters, skill queries and text search with indexes.
- **Text search: `pg_trgm` GIN index** on the description column, accepting the extra disk space.
- **Analytics: DuckDB** as an optional read-only sidecar for aggregate reports (Q5/Q6 type), loaded from exports of the primary store. It is not a second source of truth.
- **SQLite: local development and tests only.** It was the slowest on Q3, Q5 and Q6 at 300k.

Not chosen: MongoDB (not benchmarked; its main strength, flexible documents, is covered by the jsonb column, while our queries are join, aggregate and substring-search heavy), a separate search engine (extra system for a gain the trigram index already covers at this scale), `jsonb`-only layout (section 3), and DuckDB as primary store (slowest on Q1 and Q2, 6x slower than PostgreSQL on Q3 at 300k, and an in-process single-writer model).

## 8. Operational notes

- Run `VACUUM (ANALYZE)` after every bulk load. The first 300k run, without it, showed the trigram query at 331 ms; after the change it measured 36.73 ms, and an isolated check of the first run's data gave 30 to 43 ms. The cause of the first figure was not confirmed; background autovacuum after the load is the suspect.
- The same step also improved Q4 in PostgreSQL (24.64 ms before, 4.89 ms after at 300k), probably through index-only scans.
- Index build is the most expensive load step in PostgreSQL (19.69 s at 300k); plan bulk loads as load first, then index.
- Re-run `bench8.py` after any schema, index or hardware change.

## 9. Limits of this study

- One machine, one benchmark run per size after the rerun; no confidence intervals.
- Synthetic data: uniform dates, 100 skills with a skewed distribution, random words in descriptions.
- The benchmark schema is typed (two tables), not the hybrid layout; engine results (`bench8.py`) and layout results (`pg_poc.py`) come from different experiments.
- DuckDB has only an id index. SQLite and PostgreSQL have five indexes.
- SQLite load goes through Python `executemany`; DuckDB and PostgreSQL use native CSV copy, so load times are not directly comparable.
- PostgreSQL timings include a local client round trip (psql timing output).
- Validation recall and precision come from defects injected by the same generator that made the labels, so they cover known defect types only; real scraped data may fail in other ways.
- L1 timings are single-thread Python on one machine.
- Real-data validation covers only 204 raw and 203 processed records, with the content-hash check off (its recipe is unknown), so zero rejections there is a weak signal. The real data files are not part of this repository, and a database run on real data is not included in this document.

## 10. Reproduce

Prerequisites: Python 3.14 (earlier 3.x versions are untested), a local PostgreSQL 18 server with the `pg_trgm` extension available, and `psql` on the PATH.

Install the packages and generate the mock data (10,000 raw and 10,000 processed records plus a labels file):

```
pip install -r requirements.txt
python gen_mock.py --n 10000 --seed 42 --prefix mock
```

Validate it and compare the two L1 engines:

```
python validate.py --raw mock_raw.jsonl --processed mock_processed.jsonl --labels mock_labels.jsonl --engine both
```

Create the two PostgreSQL databases once (the names are the defaults of the scripts, `--pg-db` and `--db` change them):

```
psql -U postgres -c "CREATE DATABASE fanous_bench"
psql -U postgres -c "CREATE DATABASE scrape_poc"
```

Set the PostgreSQL password. On Windows cmd:

```
set PGPASSWORD=<password>
```

On PowerShell use `$env:PGPASSWORD = "<password>"` instead.

Run the storage layout comparison (section 3) and the engine benchmark (section 4):

```
python pg_poc.py --raw mock_raw.jsonl --processed mock_processed.jsonl
python bench8.py --n 100000
python bench8.py --n 300000 --out bench_out_300k
```

`lite_duck_poc.py --processed mock_processed.jsonl` runs the same Q1-Q6 on SQLite and DuckDB for the 8.8k valid mock records.

Outputs of `bench8.py`: `bench_queries.csv`, `bench_loads.csv` and `BENCHMARK.md` in the output folder.
