"""Heavy-index, light-search profile.

Asymmetric design: a very dense graph is built once (high m and ef_construct)
but each query explores very little of it (low hnsw_ef) for ultra-fast search.
Run: python heavy_index_light_search_retrieval.py
"""

from benchmark_common import Profile, run_profile

# Parameter set for this profile. All environment-specific settings (URL,
# API key, data source) are read from the .env file by the shared module.
PROFILE = Profile(
    name="heavy_index_light_search",
    # m: many links per node: dense graph
    m=64,
    # ef_construct: wide build-time list: accurate graph, slow build
    ef_construct=512,
    # hnsw_ef: tiny search-time list: ultra-fast queries
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
