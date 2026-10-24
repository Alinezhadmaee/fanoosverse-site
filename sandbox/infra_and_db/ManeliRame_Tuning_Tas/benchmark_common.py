"""Shared benchmark engine for the Qdrant tuning profiles.

What this module does:
  1. Reads settings (URL, API key, data location, query counts) from CLI
     arguments or from environment variables / a local `.env` file.
  2. Loads the sample vectors, copies them into a fresh profile-specific
     Qdrant collection configured with the profile's parameters.
  3. Measures index build time, search latency and recall (against an exact
     search on the same collection), then saves the results as JSON.

Every profile script only defines its parameter values (a `Profile`) and calls
`run_profile`. Nothing environment-specific is hardcoded here; the API key
must live in a `.env` file that is excluded from Git.

Data sources (choose one):
  * --source-collection / QDRANT_SOURCE_COLLECTION: an existing Qdrant
    collection holding the sample vectors (single unnamed vector per point).
  * --data-file / TUNING_DATA_FILE: a JSONL file, one object per line, with the
    keys "point_id" (or "id"), "vector" and (optional) "payload".
"""

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

# Load variables from a local .env file (if present) into the environment.
load_dotenv()

# Tuning constants for the benchmark engine itself (not search parameters).
UPSERT_BATCH_SIZE = 256
SCROLL_BATCH_SIZE = 512
GREEN_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class Profile:
    """Parameter set describing one search architecture."""

    name: str
    m: int                      # HNSW graph links per node
    ef_construct: int           # candidate list size while building the graph
    hnsw_ef: int                # candidate list size while searching
    full_scan_threshold_kb: int  # below this segment size Qdrant skips HNSW
    exact: bool = False         # True -> bypass HNSW with exact search
    on_disk: bool = False       # True -> HNSW graph and payload on disk


def parse_args(profile_name: str) -> argparse.Namespace:
    """Build the CLI; every default can come from an environment variable."""
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description=f"Qdrant tuning benchmark for profile '{profile_name}'."
    )
    parser.add_argument("--url", default=env("QDRANT_URL", "http://localhost:6333"))
    parser.add_argument("--api-key", default=env("QDRANT_API_KEY"))
    parser.add_argument("--source-collection", default=env("QDRANT_SOURCE_COLLECTION"))
    parser.add_argument("--data-file", default=env("TUNING_DATA_FILE"))
    parser.add_argument("--collection-prefix", default=env("TUNING_COLLECTION_PREFIX", "tuning"))
    parser.add_argument("--distance", default=env("TUNING_DISTANCE", "Cosine"),
                        choices=["Cosine", "Dot", "Euclid"])
    parser.add_argument("--top-k", type=int, default=int(env("TUNING_TOP_K", "10")))
    parser.add_argument("--num-queries", type=int, default=int(env("TUNING_NUM_QUERIES", "100")))
    parser.add_argument("--seed", type=int, default=int(env("TUNING_SEED", "42")))
    parser.add_argument("--indexing-threshold-kb", type=int,
                        default=int(env("TUNING_INDEXING_THRESHOLD_KB", "10")),
                        help="Optimizer threshold; must be low so HNSW is built on small data.")
    parser.add_argument("--results-dir", default=env("TUNING_RESULTS_DIR", "results"))
    parser.add_argument("--docker-container", default=env("QDRANT_DOCKER_CONTAINER"),
                        help="Optional container name; used to record RAM via `docker stats`.")
    args = parser.parse_args()
    # A data source is mandatory: either a Qdrant collection or a JSONL file.
    if not args.source_collection and not args.data_file:
        parser.error("Provide --source-collection or --data-file.")
    return args


def load_points(client: QdrantClient, args: argparse.Namespace):
    """Yield PointStruct objects from the configured data source."""
    # Option 1: read vectors from a local JSONL file.
    if args.data_file:
        with open(args.data_file, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                # Accept both "point_id" (team export) and "id" as the key.
                point_id = row.get("point_id", row.get("id"))
                yield models.PointStruct(
                    id=point_id, vector=row["vector"], payload=row.get("payload") or {}
                )
        return

    # Option 2: page through an existing Qdrant collection with scroll().
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=args.source_collection,
            limit=SCROLL_BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for record in records:
            # Named (multi) vectors come back as dicts; they are not supported.
            if not isinstance(record.vector, list):
                raise ValueError(
                    "Only single unnamed vectors are supported by this benchmark."
                )
            yield models.PointStruct(
                id=record.id, vector=record.vector, payload=record.payload or {}
            )
        # scroll() returns offset=None once the last page has been read.
        if offset is None:
            break


def wait_until_green(client: QdrantClient, collection: str) -> None:
    # Poll the collection status until background indexing has finished.
    deadline = time.time() + GREEN_TIMEOUT_SECONDS
    while time.time() < deadline:
        info = client.get_collection(collection)
        if info.status == models.CollectionStatus.GREEN:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Collection {collection} did not become green in time.")


def read_container_memory(container: str | None) -> str | None:
    """Return the raw memory usage string reported by `docker stats`."""
    # Memory is only measurable when Qdrant runs in a known Docker container.
    if not container:
        return None
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def percentile(values: list[float], pct: float) -> float:
    """Return the pct-th percentile using the nearest-rank method."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def run_profile(profile: Profile) -> dict:
    """Run the full benchmark for one profile and return the result dict."""
    args = parse_args(profile.name)
    client = QdrantClient(url=args.url, api_key=args.api_key, timeout=120)
    # Each profile gets its own collection so profiles never interfere.
    target = f"{args.collection_prefix}_{profile.name}"
    if target == args.source_collection:
        sys.exit("Target collection must differ from the source collection.")

    # --- Load data -----------------------------------------------------
    # Materialize all points in memory; the sample set is small (<10k rows).
    points = list(load_points(client, args))
    if not points:
        sys.exit("No points were loaded from the data source.")
    dimension = len(points[0].vector)

    # --- Build the collection (timed) ---------------------------------
    if client.collection_exists(target):
        client.delete_collection(target)

    # Index time covers collection creation, upload and background indexing.
    build_start = time.perf_counter()
    client.create_collection(
        collection_name=target,
        vectors_config=models.VectorParams(
            size=dimension,
            distance=getattr(models.Distance, args.distance.upper()),
        ),
        # Graph parameters: m, ef_construct and the full-scan threshold.
        hnsw_config=models.HnswConfigDiff(
            m=profile.m,
            ef_construct=profile.ef_construct,
            full_scan_threshold=profile.full_scan_threshold_kb,
            on_disk=profile.on_disk,
        ),
        # Low threshold so the optimizer actually builds the HNSW graph.
        optimizers_config=models.OptimizersConfigDiff(
            indexing_threshold=args.indexing_threshold_kb
        ),
        # Payload storage follows the same on-disk switch as the graph.
        on_disk_payload=profile.on_disk,
    )
    for start in range(0, len(points), UPSERT_BATCH_SIZE):
        client.upsert(target, points=points[start:start + UPSERT_BATCH_SIZE], wait=True)
    wait_until_green(client, target)
    index_seconds = time.perf_counter() - build_start

    # Verify that a graph really exists; otherwise the numbers are misleading.
    info = client.get_collection(target)
    indexed = info.indexed_vectors_count or 0
    if not profile.exact and indexed == 0:
        print("WARNING: indexed_vectors_count is 0, so HNSW was NOT built. "
              "Lower --indexing-threshold-kb or check full_scan_threshold.",
              file=sys.stderr)

    # --- Queries and ground truth -------------------------------------
    # Seeded sampling keeps the query set identical across all profiles.
    rng = random.Random(args.seed)
    sample_size = min(args.num_queries, len(points))
    query_vectors = [p.vector for p in rng.sample(points, sample_size)]

    # Ground truth: exact search on the same collection (not timed).
    exact_params = models.SearchParams(exact=True)
    truth = [
        {hit.id for hit in client.query_points(
            target, query=vec, limit=args.top_k, search_params=exact_params).points}
        for vec in query_vectors
    ]

    # --- Profile search (timed per query) -----------------------------
    # Choose search-time behaviour: exact scan or HNSW with the profile's ef.
    params = (models.SearchParams(exact=True) if profile.exact
              else models.SearchParams(hnsw_ef=profile.hnsw_ef))
    latencies_ms, recalls = [], []
    for vec, expected in zip(query_vectors, truth):
        began = time.perf_counter()
        response = client.query_points(
            target, query=vec, limit=args.top_k, search_params=params)
        latencies_ms.append((time.perf_counter() - began) * 1000.0)
        # Recall = share of the true top-k that this search also returned.
        found = {hit.id for hit in response.points}
        recalls.append(len(found & expected) / max(1, len(expected)))

    # Collect everything worth reporting into one JSON-serializable dict.
    result = {
        "profile": asdict(profile),
        "collection": target,
        "points": len(points),
        "dimension": dimension,
        "top_k": args.top_k,
        "num_queries": sample_size,
        "indexed_vectors_count": indexed,
        "index_build_seconds": round(index_seconds, 3),
        "latency_ms_avg": round(statistics.mean(latencies_ms), 3),
        "latency_ms_p95": round(percentile(latencies_ms, 95), 3),
        "recall_at_k": round(statistics.mean(recalls), 4),
        "container_memory": read_container_memory(args.docker_container),
    }

    # Persist the result so it can be copied into the documentation table.
    out_dir = Path(args.results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{profile.name}.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"Saved results to {out_file}")
    return result
