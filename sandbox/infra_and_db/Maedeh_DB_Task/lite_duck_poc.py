"""SQLite (baseline) and DuckDB PoC with the same Q1-Q6 as pg_poc.py.

SQLite : hot columns typed, lists as JSON text (json_each), rest as JSON text. Stdlib only.
DuckDB : all 41 fields typed, native lists. Loaded from a temp JSONL with read_json.
Every result is also checked against a pure-Python oracle.

Usage: python lite_duck_poc.py --processed mock_processed.jsonl [--skip-hash] [--engine sqlite|duckdb|both]
"""
import argparse, json, os, sqlite3, statistics, tempfile, time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from validate import PROC, load, parse, run

HOT = ["document_id", "collector", "collected_at", "domain", "seniority", "work_mode",
       "skills_required", "salary_min", "salary_max", "text"]
LISTS = {k for k, t in PROC.items() if parse(t)[0] == "l"}


def valid(path, skip_hash):
    rows = load(path)
    bad, _ = run("processed", rows, "pydantic", skip_hash)
    keep = [r for n, r in rows if n not in bad]
    print(f"processed: {len(keep)} valid of {len(rows)} ({len(bad)} rejected)")
    return keep


def make_params(proc):
    sk = Counter(s for r in proc for s in r["skills_required"]).most_common(1)[0][0]
    d1 = date.fromisoformat(min(r["collected_at"] for r in proc))
    return dict(id=proc[len(proc) // 2]["document_id"], skill=sk, d1=d1.isoformat(),
                d2=(d1 + timedelta(days=30)).isoformat(), domain="AI/ML", mode="remote", sen="senior")


def oracle(proc, p):
    """Expected results in pure Python (strings, same shape as the SQL output)."""
    S = lambda rows: [tuple(str(x) for x in r) for r in rows]
    r1 = [r for r in proc if r["document_id"] == p["id"]]
    q2 = sorted(r["document_id"] for r in proc if r["domain"] == p["domain"]
                and p["mode"] in r["work_mode"] and p["sen"] in r["seniority"])
    q3 = sorted(r["document_id"] for r in proc if p["skill"] in r["skills_required"])[:20]
    q4 = Counter((r["domain"], r["collector"]) for r in proc if p["d1"] <= r["collected_at"] <= p["d2"])
    q5 = Counter(s for r in proc for s in r["skills_required"])
    g = {}
    for r in proc:
        if r["salary_min"] is not None:
            g.setdefault(r["seniority"][0], []).append(r["salary_min"])
    half_up = lambda x: int(x + 0.5)
    return {
        "Q1 by id": S([(x["document_id"], x["role_title"]) for x in r1]),
        "Q2 filter": S([(i,) for i in q2]),
        "Q3 skill top20": S([(i,) for i in q3]),
        "Q4 count range": S([(d, c, n) for (d, c), n in sorted(q4.items())]),
        "Q5 top skills": S(sorted(q5.items(), key=lambda kv: (-kv[1], kv[0]))[:20]),
        "Q6 salary stats": S([(k, len(v), half_up(sum(v) / len(v)), half_up(statistics.median(v)))
                              for k, v in sorted(g.items())]),
    }


# ---------- SQLite ----------
def sqlite_setup(proc, path):
    con = sqlite3.connect(path)
    cols = ", ".join(f"{k} " + ("INTEGER" if k.startswith("salary") else "TEXT")
                     + (" PRIMARY KEY" if k == "document_id" else "") for k in HOT)
    con.execute(f"CREATE TABLE jobs ({cols}, rest TEXT NOT NULL)")
    j = lambda v: json.dumps(v, ensure_ascii=False)
    t = time.perf_counter()
    con.executemany(f"INSERT INTO jobs VALUES ({','.join('?' * 11)})",
                    ([j(r[k]) if k in LISTS else r[k] for k in HOT]
                     + [j({k: v for k, v in r.items() if k not in HOT})] for r in proc))
    con.commit(); t1 = time.perf_counter()
    con.execute("CREATE INDEX i_dom ON jobs(domain)"); con.execute("CREATE INDEX i_date ON jobs(collected_at)")
    con.execute("ANALYZE"); con.commit()
    return con, t1 - t, time.perf_counter() - t1


def sqlite_queries(p):
    has = "EXISTS (SELECT 1 FROM json_each({c}) WHERE value=?)"
    return {
        "Q1 by id": ("SELECT document_id, json_extract(rest,'$.role_title') FROM jobs WHERE document_id=?", [p["id"]]),
        "Q2 filter": (f"SELECT document_id FROM jobs WHERE domain=? AND {has.format(c='work_mode')} "
                      f"AND {has.format(c='seniority')} ORDER BY 1", [p["domain"], p["mode"], p["sen"]]),
        "Q3 skill top20": (f"SELECT document_id FROM jobs WHERE {has.format(c='skills_required')} ORDER BY 1 LIMIT 20", [p["skill"]]),
        "Q4 count range": ("SELECT domain, collector, count(*) FROM jobs WHERE collected_at BETWEEN ? AND ? "
                           "GROUP BY 1,2 ORDER BY 1,2", [p["d1"], p["d2"]]),
        "Q5 top skills": ("SELECT value, count(*) FROM jobs, json_each(jobs.skills_required) "
                          "GROUP BY value ORDER BY 2 DESC, 1 LIMIT 20", []),
        "Q6 salary stats": (
            "WITH s AS (SELECT json_extract(seniority,'$[0]') AS g, salary_min AS x, "
            "row_number() OVER (PARTITION BY json_extract(seniority,'$[0]') ORDER BY salary_min) AS rn, "
            "count(*) OVER (PARTITION BY json_extract(seniority,'$[0]')) AS n FROM jobs WHERE salary_min IS NOT NULL) "
            "SELECT g, n, CAST(avg(x)+0.5 AS INTEGER), "
            "CAST(avg(CASE WHEN rn IN ((n+1)/2,(n+2)/2) THEN x END)+0.5 AS INTEGER) FROM s GROUP BY g, n ORDER BY g", []),
    }


# ---------- DuckDB ----------
DUCKTYPE = {"str": "VARCHAR", "s": "VARCHAR", "p": "VARCHAR", "e": "VARCHAR", "i": "BIGINT", "d": "DATE", "l": "VARCHAR[]"}


def duck_setup(proc, path):
    import duckdb
    con = duckdb.connect(path)
    cols = {k: DUCKTYPE[parse(t)[0]] for k, t in PROC.items()}
    con.execute("CREATE TABLE jobs (" + ", ".join(f'"{k}" {v}' for k, v in cols.items()) + ", PRIMARY KEY (document_id))")
    tmp = Path(tempfile.mkdtemp()) / "valid.jsonl"
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in proc), encoding="utf-8")
    cdef = "{" + ", ".join(f"'{k}': '{v}'" for k, v in cols.items()) + "}"
    t = time.perf_counter()
    con.execute(f"INSERT INTO jobs SELECT * FROM read_json('{tmp.as_posix()}', format='newline_delimited', columns={cdef})")
    t1 = time.perf_counter()
    con.execute("CHECKPOINT")
    return con, t1 - t, time.perf_counter() - t1


def duck_queries(p):
    return {
        "Q1 by id": ("SELECT document_id, role_title FROM jobs WHERE document_id=?", [p["id"]]),
        "Q2 filter": ("SELECT document_id FROM jobs WHERE domain=? AND list_contains(work_mode, ?) "
                      "AND list_contains(seniority, ?) ORDER BY 1", [p["domain"], p["mode"], p["sen"]]),
        "Q3 skill top20": ("SELECT document_id FROM jobs WHERE list_contains(skills_required, ?) ORDER BY 1 LIMIT 20", [p["skill"]]),
        "Q4 count range": ("SELECT domain, collector, count(*) FROM jobs WHERE collected_at BETWEEN CAST(? AS DATE) "
                           "AND CAST(? AS DATE) GROUP BY 1,2 ORDER BY 1,2", [p["d1"], p["d2"]]),
        "Q5 top skills": ("SELECT s, count(*) FROM (SELECT unnest(skills_required) AS s FROM jobs) "
                          "GROUP BY s ORDER BY 2 DESC, 1 LIMIT 20", []),
        "Q6 salary stats": ("SELECT seniority[1], count(*), CAST(round(avg(salary_min)) AS BIGINT), "
                            "CAST(round(median(salary_min)) AS BIGINT) FROM jobs WHERE salary_min IS NOT NULL "
                            "GROUP BY 1 ORDER BY 1", []),
    }


def bench(name, con, Q, expect, reps=5):
    print(f"\n[{name}] query median ms (rows) | matches oracle")
    for q, (sql, params) in Q.items():
        rows = [tuple(str(x) for x in r) for r in con.execute(sql, params).fetchall()]
        ts = []
        for _ in range(reps):
            t = time.perf_counter(); con.execute(sql, params).fetchall(); ts.append((time.perf_counter() - t) * 1000)
        print(f"  {q:16s}{statistics.median(ts):>9.2f} [{len(rows):>4}]   {'yes' if rows == expect[q] else 'NO'}")
        if rows != expect[q]:
            print("    got     :", rows[:3]); print("    expected:", expect[q][:3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--processed", required=True); ap.add_argument("--skip-hash", action="store_true")
    ap.add_argument("--engine", default="both", choices=["sqlite", "duckdb", "both"])
    a = ap.parse_args()
    proc = valid(a.processed, a.skip_hash)
    p = make_params(proc); print("params:", p)
    expect = oracle(proc, p)
    d = tempfile.mkdtemp()
    if a.engine in ("sqlite", "both"):
        path = os.path.join(d, "poc.sqlite")
        con, tl, ti = sqlite_setup(proc, path)
        print(f"\nsqlite: load {tl:.2f}s, index {ti:.2f}s, file {os.path.getsize(path)/2**20:.1f} MB")
        bench("sqlite", con, sqlite_queries(p), expect); con.close()
    if a.engine in ("duckdb", "both"):
        path = os.path.join(d, "poc.duckdb")
        con, tl, ti = duck_setup(proc, path)
        print(f"\nduckdb: load {tl:.2f}s, checkpoint {ti:.2f}s, file {os.path.getsize(path)/2**20:.1f} MB")
        bench("duckdb", con, duck_queries(p), expect); con.close()


if __name__ == "__main__":
    main()
