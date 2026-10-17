# Qdrant Tuning Sandbox (ManeliRame_Tuning_Tas)

This folder benchmarks six Qdrant search configurations on the sample vector
database. See `QDRANT_TUNING.md` for the parameter explanations, the chosen
values and the trade-off tables.

## Files

| File | Purpose |
|---|---|
| `benchmark_common.py` | Shared engine: loads data, builds the collection, measures index time, latency and recall, saves JSON results. |
| `least_compute_retrieval.py` | Very low `m` and `ef`: lowest RAM and fastest, lowest recall. |
| `balanced_retrieval.py` | Balanced values; likely production candidate. |
| `max_accuracy_retrieval.py` | Very high `m` and `ef`: best recall, highest RAM, slower. |
| `brute_force_exact_retrieval.py` | `exact=True`; bypasses HNSW and acts as ground truth (100% recall). |
| `disk_optimized_retrieval.py` | HNSW graph and payload on disk; lower RAM, slower. |
| `heavy_index_light_search_retrieval.py` | Dense graph at build time, very small `hnsw_ef` at search time. |
| `QDRANT_TUNING.md` | Parameter guide, selected values and trade-off tables. |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Template for the local `.env` file (never commit the real one). |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your own values
```

Secrets such as `QDRANT_API_KEY` live only in `.env`, which is excluded by
`.gitignore`.

## Run

```bash
python balanced_retrieval.py
```

Each script writes its metrics to `results/<profile>.json`.
