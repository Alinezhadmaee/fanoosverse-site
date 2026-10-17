"""Disk-optimized profile.

Same graph values as the balanced profile, but the HNSW graph and the payload
are stored on disk instead of RAM. Slower queries, much lower RAM footprint.
Run: python disk_optimized_retrieval.py
"""

from benchmark_common import Profile, run_profile

# Parameter set for this profile. All environment-specific settings (URL,
# API key, data source) are read from the .env file by the shared module.
PROFILE = Profile(
    name="disk_optimized",
    # m: same as balanced so only storage differs
    m=16,
    # ef_construct: same as balanced so only storage differs
    ef_construct=100,
    # hnsw_ef: same as balanced so only storage differs
    hnsw_ef=64,
    # full_scan_threshold (KB): 10 is the lowest value Qdrant accepts; forces HNSW on tiny datasets
    full_scan_threshold_kb=10,
    # exact: False keeps approximate HNSW search
    exact=False,
    # on_disk: True stores the HNSW graph and payload on disk
    on_disk=True,
)

if __name__ == "__main__":
    # Build the collection, run the benchmark and save results/<name>.json.
    run_profile(PROFILE)
