"""Validation layers for raw and processed job ads (same rules, two L1 engines).

L1 schema   : structure, types, enums, ranges   (pydantic | jsonschema | none)
L2 record   : cross-field rules (stdlib)
L3 batch    : duplicate ids across records (stdlib); raw<->processed reconcile (info)

Usage:
  python validate.py --raw mock_raw.jsonl --processed mock_processed.jsonl \
      --labels mock_labels.jsonl --engine both
  Real data: add --skip-hash (content_hash recipe is unknown).
"""
import argparse, hashlib, json, time
from collections import Counter, defaultdict
from datetime import date
from typing import Annotated, Literal

COL = ["zhr", "mnl", "hst", "mhn", "mae"]
ID_RE = r"^job_(zhr|mnl|hst|mhn|mae)_\d+$"
HASH_RE = r"^[0-9a-f]{64}$"
SEN = ["unknown", "senior", "mid", "junior", "lead", "intern"]
EMP = ["full_time", "part_time", "contract", "internship", "project_based"]
WORK = ["onsite", "remote", "unknown", "hybrid"]
DEG = ["bachelor", "master", "doctorate", "any", "associate"]
GEN = ["any", "male", "female"]
MIL = ["any", "completed_or_exempt", "amriyeh_offered", "completed_only", "deferment_accepted"]
BEN = ["supplementary_insurance", "loan", "training_budget", "flexible_hours",
       "health_insurance", "gym", "transportation"]

# Tokens: s str(non-blank) | ns str|null | p pattern | i/ni int>=min | d/nd date |
#         e/ne enum | l/nl list (of enum or free str). Shared by both engines.
RAW = {"document_id": ("p", ID_RE), "collector": ("e", COL), "collected_at": ("d",), "text": ("s",)}
PROC = {
    "document_id": ("p", ID_RE), "collector": ("e", COL), "source_type": ("e", ["job_posting"]),
    "organization": ("ns",), "role_title": ("s",), "role_normalized": ("s",), "domain": ("s",),
    "seniority": ("l", SEN), "skills_required": ("l", None), "skills_preferred": ("l", None),
    "ai_skills": ("l", None), "tools": ("l", None), "soft_skills": ("l", None),
    "employment_type": ("nl", EMP), "work_mode": ("l", WORK), "location": ("s",),
    "experience_years_min": ("ni", 0), "language": ("l", ["fa", "en"]),
    "salary_min": ("ni", 0), "salary_max": ("ni", 0), "salary_currency": ("ne", ["Toman", "USD"]),
    "benefits": ("l", BEN), "education": ("l", None), "education_degree": ("l", DEG),
    "education_field": ("l", None), "gender_requirement": ("l", GEN),
    "military_service_requirement": ("l", MIL), "age_min": ("ni", 0), "age_max": ("ni", 0),
    "company_size_min": ("ni", 0), "company_size_max": ("ni", 0), "industry": ("ns",),
    "compensation_type": ("ns",), "internship_duration_months": ("ni", 0), "source_date": ("nd",),
    "collected_at": ("d",), "text": ("s",), "content_hash": ("p", HASH_RE),
    "evidence_notes": ("l", None), "candidate_new_skill": ("l", None), "version": ("e", ["v3"]),
}
assert len(PROC) == 41 and len(RAW) == 4
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


# ---------- L1 engines ----------
def parse(t):
    """Token -> (kind, nullable, arg). 'ns' is a free nullable string."""
    k = t[0]
    arg = t[1] if len(t) > 1 else None
    if k == "ns":
        return "str", True, None
    if k in ("ni", "nd", "ne", "nl"):
        return k[1:], True, arg
    return k, False, arg


def build_pydantic(spec, name):
    from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, ValidationError, create_model
    Date = Annotated[str, StringConstraints(pattern=DATE_RE), AfterValidator(lambda s: date.fromisoformat(s) and s)]

    def typ(t):
        k, null, arg = parse(t)
        if k == "str": core = str
        elif k == "s": core = Annotated[str, StringConstraints(pattern=r"\S")]
        elif k == "p": core = Annotated[str, StringConstraints(pattern=arg)]
        elif k == "i": core = Annotated[int, Field(ge=arg)]
        elif k == "d": core = Date
        elif k == "e": core = Literal[tuple(arg)]
        elif k == "l": core = list[Literal[tuple(arg)]] if arg else list[str]
        return (core | None) if null else core

    model = create_model(name, __config__=ConfigDict(strict=True, extra="forbid"),
                         **{f: (typ(t), ...) for f, t in spec.items()})

    def check(rec):
        try:
            model.model_validate(rec)
        except ValidationError as e:
            x = e.errors()[0]
            return f"{'.'.join(map(str, x['loc']))}:{x['type']}"
    return check


def js_schema(spec):
    def prop(t):
        k, null, arg = parse(t)
        nl = ["null"] if null else []
        if k == "str": return {"type": ["string", "null"]}
        if k == "s": return {"type": "string", "pattern": r"\S"}
        if k == "p": return {"type": "string", "pattern": arg}
        if k == "i": return {"type": ["integer"] + nl, "minimum": arg}
        if k == "d": return {"type": ["string"] + nl, "pattern": DATE_RE, "format": "date"}
        if k == "e": return {"enum": list(arg) + ([None] if null else [])}
        if k == "l": return {"type": ["array"] + nl, "items": {"enum": arg} if arg else {"type": "string"}}
    return {"type": "object", "additionalProperties": False, "required": list(spec),
            "properties": {f: prop(t) for f, t in spec.items()}}


def build_jsonschema(spec, _name):
    from jsonschema import Draft202012Validator, FormatChecker
    v = Draft202012Validator(js_schema(spec), format_checker=FormatChecker())

    def check(rec):
        e = next(iter(v.iter_errors(rec)), None)
        if e:
            return f"{'.'.join(map(str, e.absolute_path))}:{e.validator}"
    return check


def build_none(spec, _name):
    return lambda rec: None


ENGINES = {"pydantic": build_pydantic, "jsonschema": build_jsonschema, "none": build_none}


# ---------- L2 / L3 ----------
def l2(layer, r, skip_hash):
    if r["document_id"].split("_")[1] != r["collector"]:
        return "id_collector_mismatch"
    if layer == "processed":
        for lo, hi in (("salary_min", "salary_max"), ("age_min", "age_max"),
                       ("company_size_min", "company_size_max")):
            if r[lo] is not None and r[hi] is not None and r[lo] > r[hi]:
                return f"{lo}>{hi}"
        if not skip_hash and hashlib.sha256(r["text"].encode("utf-8")).hexdigest() != r["content_hash"]:
            return "hash_mismatch"


def load(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    out.append((n, json.loads(line)))
                except json.JSONDecodeError:
                    out.append((n, None))
    return out


def run(layer, rows, engine, skip_hash):
    """Returns ({line: (level, msg)}, L1 seconds)."""
    check = ENGINES[engine](RAW if layer == "raw" else PROC, layer)
    res, seen, l1t = {}, set(), 0.0
    for n, rec in rows:
        if rec is None:
            res[n] = ("L0", "bad_json"); continue
        t = time.perf_counter(); m = check(rec); l1t += time.perf_counter() - t
        if m:
            res[n] = ("L1", m); continue
        try:
            m = l2(layer, rec, skip_hash)
        except (KeyError, TypeError, AttributeError, IndexError):
            m = "l2_crash_unvalidated"  # only possible when L1 is skipped
        if m:
            res[n] = ("L2", m); continue
        if rec["document_id"] in seen:
            res[n] = ("L3", "duplicate_id")
        seen.add(rec["document_id"])
    return res, l1t


def report(layer, engine, rows, res, l1t, labels):
    n = len(rows)
    lv = Counter(v[0] for v in res.values())
    print(f"\n[{layer} | {engine}] records={n} failed={len(res)} ({len(res)/n:.1%}) "
          f"by level={dict(sorted(lv.items()))} L1={l1t/n*1e6:.0f} us/rec")
    if labels is None:
        top = Counter(v[1] for v in res.values()).most_common(5)
        print("  top errors:", top)
        return
    lab = {x["line"]: x["defect"] for x in labels if x["layer"] == layer}
    flagged = set(res)
    tp, fp, fn = len(flagged & set(lab)), len(flagged - set(lab)), len(set(lab) - flagged)
    print(f"  vs labels: TP={tp} FP={fp} FN={fn} recall={tp/max(len(lab),1):.1%} precision={tp/max(len(flagged),1):.1%}")
    by = defaultdict(Counter)
    for ln, d in lab.items():
        by[d][res[ln][0] if ln in res else "MISSED"] += 1
    for d, c in sorted(by.items()):
        print(f"  {d:20s} {dict(c)}")


def reconcile(raw_rows, proc_rows):
    r = {x["document_id"]: x for _, x in raw_rows if x and isinstance(x.get("document_id"), str)}
    p = {x["document_id"]: x for _, x in proc_rows if x and isinstance(x.get("document_id"), str)}
    only_r, only_p = sorted(set(r) - set(p)), sorted(set(p) - set(r))
    diff = [k for k in set(r) & set(p) if r[k].get("text") != p[k].get("text")]
    print(f"\n[reconcile raw<->processed] raw={len(r)} processed={len(p)} only_raw={len(only_r)} "
          f"only_processed={len(only_p)} text_mismatch={len(diff)}")
    print("  only_raw examples:", only_r[:3], "| only_processed examples:", only_p[:3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw"); ap.add_argument("--processed"); ap.add_argument("--labels")
    ap.add_argument("--engine", default="both", choices=["pydantic", "jsonschema", "both", "none"])
    ap.add_argument("--skip-hash", action="store_true")
    a = ap.parse_args()
    labels = load(a.labels) if a.labels else None
    labels = [x for _, x in labels] if labels else None
    engines = ["pydantic", "jsonschema"] if a.engine == "both" else [a.engine]
    data = {}
    for layer, path in (("raw", a.raw), ("processed", a.processed)):
        if not path:
            continue
        data[layer] = load(path)
        for eng in engines:
            res, l1t = run(layer, data[layer], eng, a.skip_hash)
            report(layer, eng, data[layer], res, l1t, labels)
    if len(data) == 2:
        reconcile(data["raw"], data["processed"])


if __name__ == "__main__":
    main()
