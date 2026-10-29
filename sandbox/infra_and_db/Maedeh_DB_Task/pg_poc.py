"""PostgreSQL PoC: three storage models, bulk load, indexes, queries Q1-Q6.

Models (one schema each, table `jobs`):
  m_jsonb  : doc_id + whole record as JSONB (GIN jsonb_path_ops)
  m_typed  : all 41 fields as typed columns (lists as text[])
  m_hybrid : hot fields typed, the rest in a JSONB column `rest`
Only records that pass validation (pydantic L1 + L2 + L3) are loaded.
Password: set $env:PGPASSWORD (read by libpq). Never put it in code.

Usage:
  python pg_poc.py --raw mock_raw.jsonl --processed mock_processed.jsonl
  python pg_poc.py --raw main-raw.jsonl --processed main-processed-v3.jsonl --skip-hash
"""
import argparse, json, statistics, time
from collections import Counter

import psycopg
from psycopg.types.json import Jsonb

from validate import PROC, load, parse, run

SEARCH = {"domain": "AI/ML", "work_mode": "remote", "seniority": "senior"}  # Q2 filter
HOT = ["document_id", "collector", "collected_at", "domain", "seniority", "work_mode",
       "skills_required", "salary_min", "salary_max", "text"]
SQLTYPE = {"str": "text", "s": "text", "p": "text", "e": "text", "i": "bigint", "d": "date", "l": "text[]"}


def col_type(tok):
    return SQLTYPE[parse(tok)[0]]


def valid(layer, path, skip_hash):
    rows = load(path)
    bad, _ = run(layer, rows, "pydantic", skip_hash)
    keep = [r for n, r in rows if n not in bad]
    print(f"{layer}: {len(keep)} valid of {len(rows)} ({len(bad)} rejected)")
    return keep


def ddl():
    typed = ",\n  ".join(f'"{k}" {col_type(t)}' + (" PRIMARY KEY" if k == "document_id" else "")
                         for k, t in PROC.items())
    hyb = ",\n  ".join(f'"{k}" {col_type(PROC[k])}' + (" PRIMARY KEY" if k == "document_id" else "")
                       for k in HOT)
    return {
        "m_jsonb": "CREATE TABLE m_jsonb.jobs (doc_id text PRIMARY KEY, doc jsonb NOT NULL)",
        "m_typed": f"CREATE TABLE m_typed.jobs (\n  {typed}\n)",
        "m_hybrid": f"CREATE TABLE m_hybrid.jobs (\n  {hyb},\n  rest jsonb NOT NULL\n)",
    }


INDEXES = {
    "m_jsonb": ["CREATE INDEX ON m_jsonb.jobs USING gin (doc jsonb_path_ops)"],
    "m_typed": ["CREATE INDEX ON m_typed.jobs (collected_at)", "CREATE INDEX ON m_typed.jobs (domain)",
                "CREATE INDEX ON m_typed.jobs USING gin (seniority)",
                "CREATE INDEX ON m_typed.jobs USING gin (work_mode)",
                "CREATE INDEX ON m_typed.jobs USING gin (skills_required)"],
}
INDEXES["m_hybrid"] = [s.replace("m_typed", "m_hybrid") for s in INDEXES["m_typed"]]


def copy_rows(cur, model, recs):
    if model == "m_jsonb":
        tbl, cols = "m_jsonb.jobs", "(doc_id, doc)"
        rows = ((r["document_id"], json.dumps(r, ensure_ascii=False)) for r in recs)
    elif model == "m_typed":
        tbl, cols = "m_typed.jobs", "(" + ",".join(f'"{k}"' for k in PROC) + ")"
        rows = ([r[k] for k in PROC] for r in recs)
    else:
        tbl, cols = "m_hybrid.jobs", "(" + ",".join(f'"{k}"' for k in HOT) + ",rest)"
        rows = ([r[k] for k in HOT] + [json.dumps({k: v for k, v in r.items() if k not in HOT},
                                                   ensure_ascii=False)] for r in recs)
    with cur.copy(f"COPY {tbl} {cols} FROM STDIN") as cp:
        for row in rows:
            cp.write_row(row)


def load_all(conn, raw, proc):
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS public.raw_docs")
    cur.execute("CREATE TABLE public.raw_docs (document_id text PRIMARY KEY, collector text, "
                "collected_at date, text text)")
    t = time.perf_counter()
    with cur.copy("COPY public.raw_docs FROM STDIN") as cp:
        for r in raw:
            cp.write_row([r["document_id"], r["collector"], r["collected_at"], r["text"]])
    print(f"raw_docs loaded in {time.perf_counter() - t:.2f}s")
    out = {}
    for model, sql in ddl().items():
        cur.execute(f"DROP SCHEMA IF EXISTS {model} CASCADE")
        cur.execute(f"CREATE SCHEMA {model}")
        cur.execute(sql)
        t0 = time.perf_counter(); copy_rows(cur, model, proc); t1 = time.perf_counter()
        for s in INDEXES[model]:
            cur.execute(s)
        cur.execute(f"ANALYZE {model}.jobs"); t2 = time.perf_counter()
        cur.execute(f"SELECT pg_relation_size('{model}.jobs'), pg_total_relation_size('{model}.jobs')")
        tab, tot = cur.fetchone()
        out[model] = dict(copy_s=t1 - t0, index_s=t2 - t1, table_mb=tab / 2**20, total_mb=tot / 2**20)
    return out


def queries(p):
    """p: dict(id, skill, d1, d2). Each query: (sql, params) per model, same output columns."""
    s = SEARCH
    arr = lambda v: [v]
    Q = {}
    Q["Q1 by id"] = {
        "m_jsonb": ("SELECT doc_id, doc->>'role_title' FROM m_jsonb.jobs WHERE doc_id=%s", [p["id"]]),
        "m_typed": ("SELECT document_id, role_title FROM m_typed.jobs WHERE document_id=%s", [p["id"]]),
        "m_hybrid": ("SELECT h.document_id, h.rest->>'role_title' FROM m_hybrid.jobs h WHERE document_id=%s", [p["id"]]),
    }
    flt = {"domain": s["domain"], "work_mode": arr(s["work_mode"]), "seniority": arr(s["seniority"])}
    sel_arr = ("SELECT document_id FROM {m}.jobs WHERE domain=%s AND work_mode @> %s::text[] "
               "AND seniority @> %s::text[] ORDER BY 1")
    Q["Q2 filter"] = {
        "m_jsonb": ("SELECT doc_id FROM m_jsonb.jobs WHERE doc @> %s ORDER BY 1", [Jsonb(flt)]),
        "m_typed": (sel_arr.format(m="m_typed"), [flt["domain"], flt["work_mode"], flt["seniority"]]),
        "m_hybrid": (sel_arr.format(m="m_hybrid"), [flt["domain"], flt["work_mode"], flt["seniority"]]),
    }
    Q["Q3 skill top20"] = {
        "m_jsonb": ("SELECT doc_id FROM m_jsonb.jobs WHERE doc @> %s ORDER BY 1 LIMIT 20",
                    [Jsonb({"skills_required": [p["skill"]]})]),
        "m_typed": ("SELECT document_id FROM m_typed.jobs WHERE skills_required @> %s::text[] ORDER BY 1 LIMIT 20", [[p["skill"]]]),
        "m_hybrid": ("SELECT document_id FROM m_hybrid.jobs WHERE skills_required @> %s::text[] ORDER BY 1 LIMIT 20", [[p["skill"]]]),
    }
    cnt = "SELECT {d}, {c}, count(*) FROM {t} WHERE {a} BETWEEN %s AND %s GROUP BY 1,2 ORDER BY 1,2"
    Q["Q4 count range"] = {
        "m_jsonb": (cnt.format(d="doc->>'domain'", c="doc->>'collector'", t="m_jsonb.jobs",
                               a="(doc->>'collected_at')::date"), [p["d1"], p["d2"]]),
        "m_typed": (cnt.format(d="domain", c="collector", t="m_typed.jobs", a="collected_at"), [p["d1"], p["d2"]]),
        "m_hybrid": (cnt.format(d="domain", c="collector", t="m_hybrid.jobs", a="collected_at"), [p["d1"], p["d2"]]),
    }
    top = "SELECT s, count(*) FROM {t}, {u} s GROUP BY s ORDER BY 2 DESC, 1 LIMIT 20"
    Q["Q5 top skills"] = {
        "m_jsonb": (top.format(t="m_jsonb.jobs", u="jsonb_array_elements_text(doc->'skills_required')"), []),
        "m_typed": (top.format(t="m_typed.jobs", u="unnest(skills_required)"), []),
        "m_hybrid": (top.format(t="m_hybrid.jobs", u="unnest(skills_required)"), []),
    }
    sal = ("SELECT {g}, count(*), round(avg({x})::numeric,0), "
           "round((percentile_cont(0.5) WITHIN GROUP (ORDER BY {x}))::numeric,0) "
           "FROM {t} WHERE {x} IS NOT NULL GROUP BY 1 ORDER BY 1")
    Q["Q6 salary stats"] = {
        "m_jsonb": (sal.format(g="doc->'seniority'->>0", x="(doc->>'salary_min')::numeric", t="m_jsonb.jobs"), []),
        "m_typed": (sal.format(g="seniority[1]", x="salary_min", t="m_typed.jobs"), []),
        "m_hybrid": (sal.format(g="seniority[1]", x="salary_min", t="m_hybrid.jobs"), []),
    }
    return Q


def bench(conn, Q, reps=5):
    cur = conn.cursor()
    sigs, med = {}, {}
    for q, per in Q.items():
        for model, (sql, params) in per.items():
            cur.execute(sql, params); rows = cur.fetchall()        # warm-up + result
            sigs[(q, model)] = [tuple(str(x) for x in r) for r in rows]
            ts = []
            for _ in range(reps):
                t = time.perf_counter(); cur.execute(sql, params); cur.fetchall()
                ts.append((time.perf_counter() - t) * 1000)
            med[(q, model)] = statistics.median(ts)
    print("\nQuery median ms (rows returned in brackets)")
    models = ["m_jsonb", "m_typed", "m_hybrid"]
    print(f"{'query':18s}" + "".join(f"{m:>18s}" for m in models) + "   same result")
    for q in Q:
        same = len({tuple(sigs[(q, m)]) for m in models}) == 1
        print(f"{q:18s}" + "".join(f"{med[(q, m)]:>11.2f} [{len(sigs[(q, m)]):>4}]" for m in models)
              + f"   {'yes' if same else 'NO'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True); ap.add_argument("--processed", required=True)
    ap.add_argument("--skip-hash", action="store_true")
    ap.add_argument("--host", default="localhost"); ap.add_argument("--db", default="scrape_poc")
    ap.add_argument("--user", default="postgres")
    a = ap.parse_args()
    raw, proc = valid("raw", a.raw, a.skip_hash), valid("processed", a.processed, a.skip_hash)
    sk = Counter(s for r in proc for s in r["skills_required"]).most_common(1)[0][0]
    dates = sorted(r["collected_at"] for r in proc)
    from datetime import date, timedelta
    d1 = date.fromisoformat(dates[0]); d2 = d1 + timedelta(days=30)
    p = dict(id=proc[len(proc) // 2]["document_id"], skill=sk, d1=d1.isoformat(), d2=d2.isoformat())
    print("params:", p)
    with psycopg.connect(host=a.host, dbname=a.db, user=a.user, autocommit=True) as conn:
        stats = load_all(conn, raw, proc)
        print(f"\n{'model':10s}{'copy s':>9s}{'index s':>9s}{'table MB':>10s}{'total MB':>10s}")
        for m, s in stats.items():
            print(f"{m:10s}{s['copy_s']:>9.2f}{s['index_s']:>9.2f}{s['table_mb']:>10.1f}{s['total_mb']:>10.1f}")
        bench(conn, queries(p))


if __name__ == "__main__":
    main()
