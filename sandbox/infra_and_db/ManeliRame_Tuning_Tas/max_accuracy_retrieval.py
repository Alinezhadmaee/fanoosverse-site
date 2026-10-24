"""Max-accuracy profile.

Very high m and ef values build a dense graph and search it widely.
Expect the best approximate recall, the highest RAM usage and slower queries.
Run: python max_accuracy_retrieval.py
"""

from benchmark_common import Profile, run_profile

# Parameter set for this profile. All environment-specific settings (URL,
# API key, data source) are read from the .env file by the shared module.
PROFILE = Profile(
    name="max_accuracy",
    # m: many links per node: dense graph, high RAM
    m=64,
    # ef_construct: wide build-time candidate list: best graph quality
    ef_construct=512,
    # hnsw_ef: wide search-time candidate list: highest recall, slower
    hnsw_ef=512,
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
