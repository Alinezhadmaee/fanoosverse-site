# Benchmark results

- Records: 100000 jobs (synthetic), repeats per query: 7 (median)
- Python 3.14.6 on Windows, date 2026-10-29
- Every answer is checked against a pure-Python oracle

## Load and size

| Engine | Load (s) | Index (s) | Size (MB) |
|---|---:|---:|---:|
| sqlite | 1.86 | 1.53 | 84.9 |
| duckdb | 1.10 | 0.11 | 23.3 |
| pg | 1.62 | 5.86 | 103.5 |

## Query median time (ms)

| Query | sqlite | duckdb | pg |
|---|---:|---:|---:|
| Q1 by id | 0.07 | 0.86 | 0.16 |
| Q2 filter | 0.17 | 6.13 | 0.36 |
| Q3 skill top20 | 67.47 | 17.16 | 3.19 |
| Q4 count range | 1.25 | 1.15 | 1.82 |
| Q5 top skills | 66.91 | 4.79 | 128.87 |
| Q6 salary stats | 119.15 | 3.68 | 94.50 |
| Q7 text search | 110.25 | 57.21 | 299.53 |
| Q7 text search (trgm) | - | - | 8.59 |

## Notes

- SQLite load goes through Python executemany; DuckDB and PostgreSQL use native CSV copy.
- DuckDB has only the id index (zone maps for the rest); SQLite and PostgreSQL have 5 indexes.
- Q7 is a substring search on raw text (full scan). PostgreSQL also gets a pg_trgm GIN index when available.
- PostgreSQL timings come from psql timing output and include a local client round trip.
- PostgreSQL size = tables + indexes of the two tables (the trigram index is not included).
- PostgreSQL runs VACUUM (ANALYZE) after load and after the trigram index, so background autovacuum does not disturb timings.
