"""
test_retrieval.py

Embeds search prompts with EmbeddingGemma (using the mandatory query prefix),
runs Qdrant searches with and without hard filters, prints the top-3 results
and writes retrieval_report.md with the REAL results and a Pass/Fail verdict
for every test.

Usage:
    python test_retrieval.py
    python test_retrieval.py --prompt "python backend developer"   # ad-hoc search
"""

import argparse
import json
import re
import sys
from datetime import datetime

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

MODEL_NAME = "google/embeddinggemma-300m"
QUERY_PREFIX = "task: search result | query: "  # mandatory by the task spec
TOP_K = 3
MIN_SALARY = 40_000_000


def embed_query(model: SentenceTransformer, prompt: str) -> list[float]:
    """Embed a user prompt with the mandatory search prefix."""
    return model.encode(QUERY_PREFIX + prompt, normalize_embeddings=True).tolist()


def has_word(keyword: str, blob: str) -> bool:
    """Whole-word match, so 'ui' does not match inside 'building'."""
    return re.search(r"(?<!\w)" + re.escape(keyword.lower()) + r"(?!\w)", blob) is not None


def as_list(value) -> list:
    """Payload fields may be a string or a list; normalize to a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ---- Payload checks used to verify that hard filters were really applied ----
def salary_ok(p: dict) -> bool:
    return p.get("salary_min") is not None and p["salary_min"] >= MIN_SALARY


def mid_ok(p: dict) -> bool:
    return "mid" in as_list(p.get("seniority"))


def onsite_ok(p: dict) -> bool:
    return "onsite" in as_list(p.get("work_mode"))


# ---- Filters ----
F_SALARY = models.FieldCondition(key="salary_min", range=models.Range(gte=MIN_SALARY))
F_MID = models.FieldCondition(key="seniority", match=models.MatchValue(value="mid"))
F_ONSITE = models.FieldCondition(key="work_mode", match=models.MatchValue(value="onsite"))

# ---- Test definitions ----
# Semantic tests pass when TOP_K results come back AND at least one of the
# top results contains one of the expected keywords (relevance check).
# Filter tests pass when EVERY returned hit satisfies the filter AND the number
# of hits equals min(TOP_K, number of points that match the filter).
TESTS = [
    {
        "name": "Pure semantic 1",
        "prompt": "python backend developer",
        "filter": None, "filter_desc": "none",
        "keywords": ["python", "backend"],
    },
    {
        "name": "Pure semantic 2",
        "prompt": "data scientist with machine learning experience",
        "filter": None, "filter_desc": "none",
        "keywords": ["data", "machine learning"],
    },
    {
        "name": "Pure semantic 3",
        "prompt": "speech recognition engineer",
        "filter": None, "filter_desc": "none",
        "keywords": ["speech"],
        "keyword_field": "role_normalized",  # check the job title only
    },
    {
        "name": "Hard filter 1",
        "prompt": "software developer",
        "filter": models.Filter(must=[F_SALARY]),
        "filter_desc": f"salary_min >= {MIN_SALARY}",
        "check": salary_ok,
    },
    {
        "name": "Hard filter 2",
        "prompt": "software developer",
        "filter": models.Filter(must=[F_MID]),
        "filter_desc": "seniority = mid",
        "check": mid_ok,
    },
    {
        "name": "Hard filter 3 (combined)",
        "prompt": "software developer",
        "filter": models.Filter(must=[F_ONSITE, F_SALARY]),
        "filter_desc": f"work_mode = onsite AND salary_min >= {MIN_SALARY}",
        "check": lambda p: onsite_ok(p) and salary_ok(p),
    },
]


NOTES = [
    "## Notes",
    "",
    "- **Vectors were re-computed.** The vectors shipped in `Vectorized_raw_data.jsonl` "
    "(768 dims, L2-normalised) are not compatible with `google/embeddinggemma-300m`: on the "
    "first 3 records the cosine between the stored vector and this model's embedding of the "
    "record text was between -0.02 and 0.07 for every text format tried (raw text, role + text, "
    "document prefix with and without title). Semantic search with this model on those vectors "
    "gave near-zero scores. `ingest_qdrant.py` therefore embeds `payload['text']` with "
    "EmbeddingGemma (document prefix `title: none | text:`). Pass `--use-file-vectors` to ingest "
    "the original vectors unchanged.",
    "- **Point IDs** are the `point_id` UUIDs from the file.",
    "- **Query prefix** `task: search result | query: ` is added to every prompt.",
    "- **Test 3 prompt** was chosen to match a role present in the data. An initial "
    "'ui ux designer' prompt was dropped because none of the 10 records is a design role.",
    "- **Limits:** the sample has only 10 records, so each filter test matched just 2 points. "
    "Semantic tests check the top-3 for expected keywords (whole-word match; Test 3 checks the "
    "job title only), which is a relevance sanity check, not a ranking-quality benchmark.",
    "",
]


def search(client, model, collection, prompt, flt=None, limit=TOP_K):
    """Embed the prompt and run a (filtered) vector search."""
    res = client.query_points(
        collection_name=collection,
        query=embed_query(model, prompt),
        query_filter=flt,
        limit=limit,
        with_payload=True,
    )
    return res.points


def evaluate(client, collection, test, hits):
    """Return (passed, reason) for one test."""
    if test["filter"] is None:
        if len(hits) < TOP_K:
            return False, f"only {len(hits)} results returned"
        field = test.get("keyword_field")
        if field:
            blob = " ".join(str((h.payload or {}).get(field, "")).lower() for h in hits)
        else:
            blob = " ".join(json.dumps(h.payload, ensure_ascii=False).lower() for h in hits)
        found = [k for k in test["keywords"] if has_word(k, blob)]
        if found:
            return True, f"relevant keywords found in top-{TOP_K}: {found}"
        return False, "none of the expected keywords appear in the top results"

    matching = client.count(collection, count_filter=test["filter"], exact=True).count
    expected = min(TOP_K, matching)
    bad = [h.id for h in hits if not test["check"](h.payload)]
    if matching == 0:
        return False, "no point in the collection matches this filter"
    if bad:
        return False, f"hits violating the filter: {bad}"
    if len(hits) != expected:
        return False, f"got {len(hits)} hits, expected {expected}"
    return True, f"all {len(hits)} hits satisfy the filter ({matching} points match overall)"


def short_row(hit) -> str:
    p = hit.payload or {}
    return f"{hit.id} | score={hit.score:.4f} | {p.get('role_normalized')}"



NOTES = [
    "## Notes",
    "",
    "**Embedding method.** Search prompts are embedded with `google/embeddinggemma-300m` "
    "using the mandatory prefix `task: search result | query: `. Documents are embedded from "
    "`payload[\"text\"]` with the model's document prompt (`title: none | text: ...`). "
    "All vectors are 768-dimensional, L2-normalised, and compared with cosine distance.",
    "",
    "**Why the vectors in the JSONL file were not used.** The `vector` field of "
    "`Vectorized_raw_data.jsonl` is not compatible with EmbeddingGemma. For three sampled records, "
    "the cosine similarity between the stored vector and a fresh EmbeddingGemma embedding of the "
    "same text (title only, full text, with and without the document prefix) was between about "
    "-0.02 and 0.07, i.e. unrelated. The stored vectors are normalised and very similar to each "
    "other (pairwise cosine: mean 0.84, min 0.75, max 0.95 over the 10 records), which suggests "
    "they were produced by a different model that also outputs 768 dimensions. With them, "
    "semantic search scored about 0.0 for every prompt and the ranking was not meaningful. "
    "`ingest_qdrant.py` therefore re-embeds `payload[\"text\"]` with EmbeddingGemma by default; "
    "pass `--use-file-vectors` to ingest the original vectors unchanged.",
    "",
    "**Dataset size.** Only 10 records are available. Each filter test matches just 2 points, so "
    "the filters are verified to be applied correctly, but the filtered rankings are not a strong "
    "test of relevance.",
    "",
    "**Pass criteria.** Semantic tests pass when 3 results are returned and at least one "
    "expected whole-word keyword appears in the top-3 results (for test 3, in the job title). "
    "Filter tests pass when every returned hit satisfies the filter and the number of hits equals "
    "`min(3, number of points matching the filter)`.",
    "",
]


def write_report(results, path, collection):
    passed = sum(r["passed"] for r in results)
    lines = [
        "# Retrieval Report",
        "",
        f"- Date: {datetime.now():%Y-%m-%d}",
        f"- Collection: `{collection}`",
        f"- Model: `{MODEL_NAME}`",
        f"- Query prefix: `{QUERY_PREFIX}{{prompt}}`",
        f"- Result: **{passed}/{len(results)} tests passed**",
        "",
    ]
    lines += NOTES
    for i, r in enumerate(results, start=1):
        lines += [
            f"## Test {i}: {r['name']}",
            "",
            f"- Prompt: `{r['prompt']}`",
            f"- Filter: {r['filter_desc']}",
            "",
            "| Rank | Point ID | Score | Role |",
            "|---|---|---|---|",
        ]
        for rank, h in enumerate(r["hits"], start=1):
            role = (h.payload or {}).get("role_normalized")
            lines.append(f"| {rank} | `{h.id}` | {h.score:.4f} | {role} |")
        verdict = "PASS" if r["passed"] else "FAIL"
        lines += ["", f"- Verdict: **{verdict}**: {r['reason']}", ""]
    lines += NOTES
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--collection", default="jobs")
    parser.add_argument("--report", default="retrieval_report.md")
    parser.add_argument("--prompt", help="run a single ad-hoc search and exit")
    parser.add_argument("--list", action="store_true", help="list all records (id, role, filters) and exit")
    args = parser.parse_args()

    client = QdrantClient(url=args.url)

    if args.list:
        pts, _ = client.scroll(args.collection, limit=1000, with_payload=True)
        for p in pts:
            pl = p.payload or {}
            print(p.id, "|", pl.get("role_normalized"), "|", pl.get("seniority"),
                  "|", pl.get("work_mode"), "|", pl.get("salary_min"))
        return 0

    model = SentenceTransformer(MODEL_NAME)

    if args.prompt:
        for h in search(client, model, args.collection, args.prompt):
            print(short_row(h))
        return 0

    results = []
    for test in TESTS:
        hits = search(client, model, args.collection, test["prompt"], test["filter"])
        passed, reason = evaluate(client, args.collection, test, hits)
        results.append({**test, "hits": hits, "passed": passed, "reason": reason})

        print(f"\n=== {test['name']} ===")
        print(f"prompt : {test['prompt']}")
        print(f"filter : {test['filter_desc']}")
        for rank, h in enumerate(hits, start=1):
            print(f"  {rank}. {short_row(h)}")
        print(f"  -> {'PASS' if passed else 'FAIL'}: {reason}")

    write_report(results, args.report, args.collection)
    total = sum(r["passed"] for r in results)
    print(f"\n{total}/{len(results)} tests passed. Report written to {args.report}")
    return 0 if total == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
