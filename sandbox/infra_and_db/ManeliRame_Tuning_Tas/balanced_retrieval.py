"""Balanced profile.

Middle-of-the-road values that trade speed, memory and recall evenly.
This is the most likely candidate for production use.
Run: python balanced_retrieval.py
"""

from benchmark_common import Profile, run_profile

# Parameter set for this profile. All environment-specific settings (URL,
# API key, data source) are read from the .env file by the shared module.
PROFILE = Profile(
    name="balanced",
    # m: common default-like graph density
    m=16,
    # ef_construct: solid graph quality at moderate build cost
    ef_construct=100,
    # hnsw_ef: good recall with moderate query latency
    hnsw_ef=64,
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
