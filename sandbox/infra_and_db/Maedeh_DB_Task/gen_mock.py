"""Seeded mock generator for scraped job ads (stdlib only).

Two layers, modeled on the real files:
  raw        4 fields: document_id, collector, collected_at, text
  processed  41 fields (all keys present, missing values as null / [])
Defects are injected at --defect-rate; labels go to <prefix>_labels.jsonl.

Usage: python gen_mock.py --n 10000 --seed 42 --prefix mock
"""
import argparse, hashlib, json, random
from datetime import date, timedelta

COLLECTORS = ["zhr", "mnl", "hst", "mhn", "mae"]
DOMAINS = (["AI/ML"] * 82 + ["Data Analytics"] * 11 + ["Data Science"] * 2 + ["Data Engineering"] * 2
           + ["Business Intelligence"] * 2 + ["Software Engineering"])
SENIORITY = ["unknown"] * 41 + ["senior"] * 32 + ["mid"] * 21 + ["junior"] * 12 + ["lead", "intern"]
EMP = ["full_time"] * 90 + ["part_time"] * 4 + ["contract"] * 3 + ["internship"] * 2 + ["project_based"]
WORK = ["onsite"] * 84 + ["remote"] * 16 + ["unknown"] * 3 + ["hybrid"] * 2
DEGREE = ["bachelor"] * 43 + ["master"] * 28 + ["doctorate"] * 20 + ["any"] * 8 + ["associate"]
MIL = ["any"] * 16 + ["completed_or_exempt"] * 13 + ["amriyeh_offered"] * 6 + ["completed_only"] * 3 + ["deferment_accepted"] * 2
BENEFITS = ["supplementary_insurance", "loan", "training_budget", "flexible_hours",
            "health_insurance", "gym", "transportation"]
SKILLS = [f"skill_{i}" for i in range(300)]
INDUSTRY = ["fintech", "ecommerce", "food_delivery", "healthtech", "b2b_saas", "finance_trading"]
COMP = ["commission", "revenue_share", "equity"]

REQUIRED = ["document_id", "collector", "collected_at", "text", "role_title", "domain", "content_hash"]
ENUM_FIELDS = {"domain": set(DOMAINS), "seniority": set(SENIORITY), "employment_type": set(EMP),
               "work_mode": set(WORK), "education_degree": set(DEGREE)}


def fa(rng, words):
    """Persian-like text from code points (no Persian literals in source)."""
    letters = [chr(c) for c in range(0x0627, 0x064B)]
    return " ".join("".join(rng.choice(letters) for _ in range(rng.randint(2, 8)))
                    for _ in range(words))


def lst(rng, pool, n):
    return rng.sample(pool, min(n, len(pool)))


def opt(rng, p, fn):
    return fn() if rng.random() < p else None


def make_pair(rng, i, base):
    col = rng.choice(COLLECTORS)
    doc_id = f"job_{col}_{i:04d}"
    collected = (base + timedelta(days=rng.randint(0, 60))).isoformat()
    n_words = max(50, min(900, int(rng.lognormvariate(5.6, 0.5))))
    title = fa(rng, rng.randint(2, 4))
    text = title + "\n" + "\n".join(fa(rng, rng.randint(3, 12)) for _ in range(n_words // 8 + 1))
    raw = {"document_id": doc_id, "collector": col, "collected_at": collected, "text": text}

    smin = opt(rng, 0.20, lambda: rng.randint(10, 150) * 1_000_000)
    smax = opt(rng, 0.15, lambda: (smin or 10_000_000) + rng.randint(0, 60) * 1_000_000)
    cs = opt(rng, 0.07, lambda: rng.randint(1, 50))
    proc = {
        "document_id": doc_id, "collector": col, "source_type": "job_posting",
        "organization": opt(rng, 0.97, lambda: fa(rng, 2)),
        "role_title": title,
        "role_normalized": f"role_{rng.randint(1, 80)}",
        "domain": rng.choice(DOMAINS),
        "seniority": [rng.choice(SENIORITY)],
        "skills_required": lst(rng, SKILLS, max(1, int(rng.gauss(11, 6)))),
        "skills_preferred": lst(rng, SKILLS, rng.randint(1, 8)) if rng.random() > 0.46 else [],
        "ai_skills": lst(rng, SKILLS, rng.randint(1, 14)) if rng.random() > 0.11 else [],
        "tools": lst(rng, SKILLS, rng.randint(1, 10)) if rng.random() > 0.08 else [],
        "soft_skills": lst(rng, SKILLS, rng.randint(1, 5)) if rng.random() > 0.34 else [],
        "employment_type": opt(rng, 0.97, lambda: [rng.choice(EMP)]),
        "work_mode": [rng.choice(WORK)],
        "location": fa(rng, 1),
        "experience_years_min": opt(rng, 0.75, lambda: rng.randint(0, 10)),
        "language": ["fa"] if rng.random() < 0.75 else ["en"],
        "salary_min": smin, "salary_max": smax,
        "salary_currency": ("Toman" if rng.random() < 0.97 else "USD") if smin else None,
        "benefits": lst(rng, BENEFITS, rng.randint(0, 4)),
        "education": [fa(rng, 2)] if rng.random() > 0.48 else [],
        "education_degree": [rng.choice(DEGREE)],
        "education_field": lst(rng, SKILLS, rng.randint(1, 4)) if rng.random() > 0.61 else [],
        "gender_requirement": [rng.choice(["any"] * 10 + ["male", "female"])],
        "military_service_requirement": [rng.choice(MIL)],
        "age_min": opt(rng, 0.18, lambda: rng.randint(18, 30)),
        "age_max": opt(rng, 0.20, lambda: rng.randint(30, 50)),
        "company_size_min": cs, "company_size_max": (cs + rng.randint(1, 100)) if cs else None,
        "industry": opt(rng, 0.05, lambda: rng.choice(INDUSTRY)),
        "compensation_type": opt(rng, 0.05, lambda: rng.choice(COMP)),
        "internship_duration_months": opt(rng, 0.015, lambda: rng.randint(1, 6)),
        "source_date": opt(rng, 0.01, lambda: (base - timedelta(days=rng.randint(1, 20))).isoformat()),
        "collected_at": collected, "text": text,
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "evidence_notes": [fa(rng, 6) for _ in range(rng.randint(1, 10))],
        "candidate_new_skill": lst(rng, SKILLS, rng.randint(1, 4)) if rng.random() > 0.65 else [],
        "version": "v3",
    }
    assert len(proc) == 41
    return raw, proc


RAW_DEFECTS = ["missing_text", "empty_text", "bad_date", "bad_collector", "dup_id", "extra_field"]
PROC_DEFECTS = ["missing_key", "bad_type", "salary_inverted", "bad_date", "bad_enum", "empty_title",
                "negative_number", "dup_id", "hash_mismatch", "collector_mismatch", "scalar_for_list"]


def defect_raw(rng, r, ids):
    k = rng.choice(RAW_DEFECTS)
    if k == "missing_text": r.pop("text")
    elif k == "empty_text": r["text"] = "  "
    elif k == "bad_date": r["collected_at"] = "31/31/2099"
    elif k == "bad_collector": r["collector"] = "xxx"
    elif k == "dup_id" and ids:
        r["document_id"] = rng.choice(ids)
        r["collector"] = r["document_id"].split("_")[1]  # keep L2 prefix rule satisfied
    elif k == "extra_field": r["unexpected"] = 1
    return k


def defect_proc(rng, p, ids):
    k = rng.choice(PROC_DEFECTS)
    if k == "missing_key": p.pop(rng.choice(REQUIRED))
    elif k == "bad_type": p["experience_years_min"] = "five"
    elif k == "salary_inverted": p["salary_min"], p["salary_max"] = 90_000_000, 10_000_000
    elif k == "bad_date": p["source_date"] = "31/31/2099"
    elif k == "bad_enum": p["work_mode"] = ["teleport"]
    elif k == "empty_title": p["role_title"] = ""
    elif k == "negative_number": p["age_min"] = -5
    elif k == "dup_id" and ids:
        p["document_id"] = rng.choice(ids)
        p["collector"] = p["document_id"].split("_")[1]
    elif k == "hash_mismatch": p["content_hash"] = "0" * 64
    elif k == "collector_mismatch": p["collector"] = "xxx"
    elif k == "scalar_for_list": p["skills_required"] = "python"
    return k


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10000)
    ap.add_argument("--defect-rate", type=float, default=0.12)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--prefix", default="mock")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    base = date(2026, 8, 1)
    ids, nbad = [], [0, 0]
    with open(f"{a.prefix}_raw.jsonl", "w", encoding="utf-8") as fr, \
         open(f"{a.prefix}_processed.jsonl", "w", encoding="utf-8") as fp, \
         open(f"{a.prefix}_labels.jsonl", "w", encoding="utf-8") as fl:
        for i in range(a.n):
            raw, proc = make_pair(rng, i + 1, base)
            prev = list(ids)
            ids.append(raw["document_id"])
            for layer, rec, fn, f in (("raw", raw, defect_raw, fr), ("processed", proc, defect_proc, fp)):
                if rng.random() < a.defect_rate:
                    kind = fn(rng, rec, prev[-500:])
                    fl.write(json.dumps({"line": i + 1, "layer": layer, "defect": kind}) + "\n")
                    nbad[layer == "processed"] += 1
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{a.n} pairs; defects raw {nbad[0]} ({nbad[0]/a.n:.1%}), processed {nbad[1]} ({nbad[1]/a.n:.1%})")


if __name__ == "__main__":
    main()
