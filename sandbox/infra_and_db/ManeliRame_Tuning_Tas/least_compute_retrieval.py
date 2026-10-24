"""Least-compute profile.

Uses very small m and ef values to keep RAM and CPU usage as low as possible.
Expect the fastest queries and the lowest recall of all graph profiles.
Run: python least_compute_retrieval.py
"""

from benchmark_common import Profile, run_profile

# Parameter set for this profile. All environment-specific settings (URL,
# API key, data source) are read from the .env file by the shared module.
PROFILE = Profile(
    name="least_compute",
    # m: very few graph links per node: minimal RAM
    m=4,
    # ef_construct: tiny build-time candidate list: fast, rough graph
    ef_construct=16,
    # hnsw_ef: tiny search-time candidate list: fast, low recall
    hnsw_ef=8,
    # full_scan_threshold (KB): 10 is the lowest value Qdrant accepts; forces HNSW on tiny datasets
    full_scan_threshold_kb=10,
    # exact: False keeps approximate HNSW search
    exact=False,
    # on_disk: False keeps everything in RAM
    on_disk=False,
)

if __name__ == "__main__":
    # Build the collection, run the benchmark and save results/<name>.json.
    run_profile(PROFILE)
