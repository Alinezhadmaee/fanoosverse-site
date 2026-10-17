"""Brute-force exact profile.

Search runs with exact=True, which bypasses HNSW completely and scans every
vector. Recall is 100% by definition, so this profile is the ground truth.
Run: python brute_force_exact_retrieval.py
"""

from benchmark_common import Profile, run_profile

# Parameter set for this profile. All environment-specific settings (URL,
# API key, data source) are read from the .env file by the shared module.
PROFILE = Profile(
    name="brute_force_exact",
    # m: unused for exact search; left at a neutral value
    m=16,
    # ef_construct: unused for exact search; left at a neutral value
    ef_construct=100,
    # hnsw_ef: unused: exact=True ignores hnsw_ef
    hnsw_ef=64,
    # full_scan_threshold (KB): default value; graph is bypassed anyway
    full_scan_threshold_kb=10000,
    # exact: True bypasses HNSW and scans everything
    exact=True,
    # on_disk: False keeps everything in RAM
    on_disk=False,
)

if __name__ == "__main__":
    # Build the collection, run the benchmark and save results/<name>.json.
    run_profile(PROFILE)
