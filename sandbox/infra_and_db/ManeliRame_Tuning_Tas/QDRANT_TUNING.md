# Qdrant Search Profile Tuning

This document describes six Qdrant configurations built to study the trade-offs
between RAM/CPU/disk usage, index build time, query latency and recall.

## 1. Parameters in plain words

| Parameter | Meaning | Higher value means |
|---|---|---|
| `m` | Number of links each node keeps in the HNSW graph. | Denser graph, better recall, more RAM, slower build. |
| `ef_construct` | Size of the candidate list used while building the graph. | Better graph quality, slower index build. |
| `hnsw_ef` | Size of the candidate list used while searching. | Higher recall, slower queries. |
| `full_scan_threshold` | Segment size (KB) under which Qdrant skips HNSW and scans everything. | Small datasets get brute-forced. |
| `indexing_threshold` | Segment size (KB) at which the optimizer builds the HNSW index. | Too high means no graph is built on small data. |
| `on_disk` | Keeps the HNSW graph and the payload on disk instead of RAM. | Lower RAM, slower queries. |
| `exact` (search param) | Forces a full exact scan and ignores HNSW. | 100% recall, slowest at scale. |

**Small-dataset caveat:** the sample database has fewer than 10,000 records, so
Qdrant would normally scan it fully. All graph-based profiles therefore set
`full_scan_threshold=10` (the minimum value Qdrant Cloud accepts; 0 is rejected) and use a low `indexing_threshold` so that the HNSW graph
is really built. Each run prints a warning if `indexed_vectors_count` is 0.

## 2. Selected values

| Script | m | ef_construct | hnsw_ef | full_scan_threshold (KB) | exact | on_disk |
|---|---|---|---|---|---|---|
| `least_compute_retrieval.py` | 4 | 16 | 8 | 10 | no | no |
| `balanced_retrieval.py` | 16 | 100 | 64 | 10 | no | no |
| `max_accuracy_retrieval.py` | 64 | 512 | 512 | 10 | no | no |
| `brute_force_exact_retrieval.py` | 16 (unused) | 100 (unused) | n/a | 10000 (default) | yes | no |
| `disk_optimized_retrieval.py` | 16 | 100 | 64 | 10 | no | yes (HNSW graph, payload) |
| `heavy_index_light_search_retrieval.py` | 64 | 512 | 8 | 10 | no | no |

Reasoning: `balanced` uses values close to Qdrant defaults and common practice;
`least_compute` and `max_accuracy` sit at the low and high ends of useful ranges;
`disk_optimized` reuses balanced values so the only difference is storage (HNSW graph and payload on disk, as the task specifies);
`heavy_index_light_search` pays the cost once at build time for a fast search.

## 3. How to run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in QDRANT_URL, QDRANT_API_KEY and one data source

python least_compute_retrieval.py
python balanced_retrieval.py
python max_accuracy_retrieval.py
python brute_force_exact_retrieval.py
python disk_optimized_retrieval.py
python heavy_index_light_search_retrieval.py
```

All settings come from the `.env` file (never committed). Set
`QDRANT_SOURCE_COLLECTION` to read vectors from an existing collection, or
`TUNING_DATA_FILE` to read a JSONL file with `point_id` (or `id`), `vector` and optional `payload` keys. Optionally set
`QDRANT_DOCKER_CONTAINER` to record RAM usage.

Each script copies the data into its own collection
(`<prefix>_<profile>`), measures index time, computes ground truth with an exact
search on that same collection, runs the profile search, and writes
`results/<profile>.json`. Other options: `--top-k`, `--num-queries`, `--seed`,
`--distance`, `--indexing-threshold-kb`, `--results-dir`.

## 4. Expected trade-offs (qualitative, on large datasets)

| Profile | RAM | CPU / latency | Disk | Index time | Recall |
|---|---|---|---|---|---|
| Least compute | Lowest | Fast | Low | Fast | Low |
| Balanced | Medium | Medium | Medium | Medium | High |
| Max accuracy | Highest | Slowest graph search | Medium | Slow | Highest approximate |
| Brute force exact | Medium | Slowest at scale | Low | Fast | 100% (ground truth) |
| Disk optimized | Low | Slower (disk reads) | High use | Medium | High |
| Heavy index, light search | High | Very fast search | Medium | Slowest | Medium to high |

## 5. Measured results

Test setup: Qdrant Cloud free tier (v1.19.2, 1 GiB RAM, 0.5 vCPU, AWS sa-east-1),
10 vectors of dimension 768, `top_k=3`, 10 queries per profile, one run per
profile (`python <script>.py --top-k 3`). Recall is measured against an exact
search on the same collection. `indexed_vectors_count` was 10 in every profile,
so the HNSW index was actually built.

| Profile | Index time (s) | Avg latency (ms) | P95 latency (ms) | Recall@3 | Memory |
|---|---|---|---|---|---|
| least_compute | 2.251 | 228.608 | 297.973 | 1.0 | not measured |
| balanced | 1.728 | 266.007 | 379.954 | 1.0 | not measured |
| max_accuracy | 1.673 | 256.997 | 502.115 | 1.0 | not measured |
| brute_force_exact | 1.298 | 223.831 | 345.953 | 1.0 | not measured |
| disk_optimized | 1.142 | 202.795 | 286.129 | 1.0 | not measured |
| heavy_index_light_search | 1.459 | 210.080 | 375.829 | 1.0 | not measured |

## 6. Limitations and honest reading of the results

- **Tiny dataset.** With only 10 vectors and `top_k=3`, every profile returns
  the exact same neighbours, so recall is 1.0 everywhere. This run cannot show
  the recall differences between the profiles.
- **Latency is dominated by the network.** All latencies are about 200 to 270 ms
  because each query travels over the internet to a remote cluster. The search
  itself on 10 vectors takes far less. The differences between profiles are
  within normal network noise and should not be read as real performance gaps.
- **Single run, few queries.** Each profile was run once with 10 queries, so the
  index times and P95 values (for example the higher P95 of max_accuracy) are
  not statistically meaningful.
- **Memory was not measured.** The script can record container memory through
  Docker, but Qdrant Cloud exposes no such number to the script. RAM and CPU
  comparisons are therefore based on the theory in section 4 only.
- **Next step.** To see real trade-offs, repeat the run with a dataset of tens
  of thousands of vectors and a local Qdrant instance (Docker), where memory can
  be recorded. The scripts support this without changes: set `QDRANT_URL` and
  `QDRANT_DOCKER_CONTAINER` in `.env`.
