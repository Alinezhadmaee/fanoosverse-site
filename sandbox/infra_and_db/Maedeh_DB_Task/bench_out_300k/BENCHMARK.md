# Benchmark results

- Records: 300000 jobs (synthetic), repeats per query: 7 (median)
- Python 3.14.6 on Windows, date 2026-10-29
- Every answer is checked against a pure-Python oracle

## Load and size

| Engine | Load (s) | Index (s) | Size (MB) |
|---|---:|---:|---:|
| sqlite | 7.49 | 10.14 | 255.2 |
| duckdb | 3.01 | 0.32 | 68.5 |
| pg | 5.79 | 19.69 | 306.3 |

## Query median time (ms)

| Query | sqlite | duckdb | pg |
|---|---:|---:|---:|
| Q1 by id | 0.10 | 0.98 | 0.17 |
| Q2 filter | 0.84 | 8.57 | 0.51 |
| Q3 skill top20 | 258.50 | 43.76 | 7.29 |
| Q4 count range | 4.29 | 2.32 | 4.89 |
| Q5 top skills | 342.30 | 8.83 | 265.03 |
| Q6 salary stats | 474.91 | 7.84 | 142.11 |
| Q7 text search | 375.78 | 123.65 | 486.90 |
| Q7 text search (trgm) | - | - | 36.73 |

## Notes

- SQLite load goes through Python executemany; DuckDB and PostgreSQL use native CSV copy.
- DuckDB has only the id index (zone maps for the rest); SQLite and PostgreSQL have 5 indexes.
- Q7 is a substring search on raw text (full scan). PostgreSQL also gets a pg_trgm GIN index when available.
- PostgreSQL timings come from psql timing output and include a local client round trip.
- PostgreSQL size = tables + indexes of the two tables (the trigram index is not included).
- PostgreSQL runs VACUUM (ANALYZE) after load and after the trigram index, so background autovacuum does not disturb timings.
