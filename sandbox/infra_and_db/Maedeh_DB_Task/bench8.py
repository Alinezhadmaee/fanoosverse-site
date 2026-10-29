"""Benchmark: SQLite vs DuckDB vs PostgreSQL on synthetic job ads.

Examples (cmd):
  python bench8.py --n 20000 --engines sqlite,duckdb
  python bench8.py --n 100000 --pg-db fanous_bench --pg-user postgres
PostgreSQL password: set PGPASSWORD before running.
Needs only the standard library; duckdb is optional; PostgreSQL uses the psql CLI.
"""
import argparse
import collections
import csv
import datetime as dt
import os
import platform
import random
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DOMAINS = ["AI/ML", "Backend", "Frontend", "Data", "DevOps", "Mobile", "Security", "QA"]
MODES = ["remote", "hybrid", "onsite"]
SENIORITY = ["junior", "mid", "senior", "lead"]
N_SKILLS = 100
KEYWORD = "kubernetes"
D_START = dt.date(2026, 1, 1)


# ---------- data + oracle ----------

def make_data(n, seed, outdir):
    """Write jobs.csv and skills.csv and compute oracle answers in pure Python."""
    rng = random.Random(seed)
    letters = "abcdefghijklmnopqrstuvwxyz"
    vocab = ["".join(rng.choice(letters) for _ in range(rng.randint(4, 9))) for _ in range(2000)]
    skill_names = ["skill_%02d" % i for i in range(N_SKILLS)]
    P = {
        "id": "job_mae_%07d" % max(1, n // 2),
        "domain": "AI/ML", "mode": "remote", "sen": "senior",
        "d1": "2026-08-01", "d2": "2026-08-31",
        "skill": "skill_01",
        "q4a": "2026-03-01", "q4b": "2026-05-31",
    }
    q1 = []
    q2 = []
    q3 = []
    q4 = 0
    q5 = collections.Counter()
    q6 = {}
    q7 = 0
    with open(outdir / "jobs.csv", "w", newline="", encoding="utf-8") as fj, \
         open(outdir / "skills.csv", "w", newline="", encoding="utf-8") as fs:
        wj = csv.writer(fj)
        ws = csv.writer(fs)
        wj.writerow(["id", "title", "domain", "mode", "seniority", "posted_date",
                     "salary_min", "salary_max", "description"])
        ws.writerow(["job_id", "skill"])
        for i in range(1, n + 1):
            jid = "job_mae_%07d" % i
            domain = rng.choice(DOMAINS)
            mode = rng.choice(MODES)
            sen = rng.choice(SENIORITY)
            day = (D_START + dt.timedelta(days=rng.randrange(365))).isoformat()
            smin = rng.randrange(20, 80) * 100
            smax = smin + rng.randrange(5, 40) * 100
            words = rng.choices(vocab, k=30)
            if rng.random() < 0.03:
                words[rng.randrange(30)] = KEYWORD
            desc = " ".join(words)
            title = "%s %s engineer" % (sen, domain)
            wj.writerow([jid, title, domain, mode, sen, day, smin, smax, desc])
            chosen = set()
            k = rng.randint(3, 8)
            while len(chosen) < k:
                chosen.add(int(rng.random() ** 2 * N_SKILLS))
            for s in sorted(chosen):
                ws.writerow([jid, skill_names[s]])
                q5[skill_names[s]] += 1
            # oracle bookkeeping
            if jid == P["id"]:
                q1.append((jid, smin, smax))
            if domain == P["domain"] and mode == P["mode"] and sen == P["sen"] \
                    and P["d1"] <= day <= P["d2"]:
                q2.append((jid,))
            if P["skill"] in {skill_names[s] for s in chosen}:
                q3.append((day, jid))
            if P["q4a"] <= day <= P["q4b"]:
                q4 += 1
            g = q6.setdefault(sen, [0, 0, 10 ** 9, 0])
            g[0] += 1
            g[1] += smax
            g[2] = min(g[2], smin)
            g[3] = max(g[3], smax)
            if KEYWORD in desc:
                q7 += 1
    q3.sort(key=lambda t: t[1])
    q3.sort(key=lambda t: t[0], reverse=True)
    oracle = {
        "Q1": q1,
        "Q2": sorted(q2),
        "Q3": [(t[1],) for t in q3[:20]],
        "Q4": [(q4,)],
        "Q5": sorted(q5.items(), key=lambda kv: (-kv[1], kv[0]))[:20],
        "Q6": [(s, g[0], g[1] / g[0], g[2], g[3]) for s, g in sorted(q6.items())],
        "Q7": [(q7,)],
    }
    return P, oracle


# ---------- queries ----------

def queries(P, like):
    return [
        ("Q1 by id",
         "SELECT id, salary_min, salary_max FROM jobs WHERE id = '%s'" % P["id"], "sii"),
        ("Q2 filter",
         "SELECT id FROM jobs WHERE domain = '%s' AND mode = '%s' AND seniority = '%s' "
         "AND posted_date BETWEEN '%s' AND '%s' ORDER BY id"
         % (P["domain"], P["mode"], P["sen"], P["d1"], P["d2"]), "s"),
        ("Q3 skill top20",
         "SELECT j.id FROM jobs j JOIN job_skills s ON s.job_id = j.id "
         "WHERE s.skill = '%s' ORDER BY j.posted_date DESC, j.id LIMIT 20" % P["skill"], "s"),
        ("Q4 count range",
         "SELECT count(*) FROM jobs WHERE posted_date BETWEEN '%s' AND '%s'"
         % (P["q4a"], P["q4b"]), "i"),
        ("Q5 top skills",
         "SELECT skill, count(*) AS c FROM job_skills GROUP BY skill "
         "ORDER BY c DESC, skill LIMIT 20", "si"),
        ("Q6 salary stats",
         "SELECT seniority, count(*), avg(salary_max), min(salary_min), max(salary_max) "
         "FROM jobs GROUP BY seniority ORDER BY seniority", "sifii"),
        ("Q7 text search",
         "SELECT count(*) FROM jobs WHERE description %s '%%%s%%'" % (like, KEYWORD), "i"),
    ]


def ddl(date_type):
    return [
        "CREATE TABLE jobs (id TEXT, title TEXT, domain TEXT, mode TEXT, seniority TEXT, "
        "posted_date %s, salary_min INTEGER, salary_max INTEGER, description TEXT)" % date_type,
        "CREATE TABLE job_skills (job_id TEXT, skill TEXT)",
    ]


INDEXES = [
    "CREATE UNIQUE INDEX ix_jobs_id ON jobs (id)",
    "CREATE INDEX ix_jobs_filter ON jobs (domain, mode, seniority, posted_date)",
    "CREATE INDEX ix_jobs_date ON jobs (posted_date)",
    "CREATE INDEX ix_skills_skill ON job_skills (skill, job_id)",
    "CREATE INDEX ix_skills_job ON job_skills (job_id)",
]


def conv(rows, kinds):
    f = {"s": str, "i": int, "f": float}
    return [tuple(f[k](v) for k, v in zip(kinds, r)) for r in rows]


def same(a, b):
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if len(ra) != len(rb):
            return False
        for x, y in zip(ra, rb):
            if isinstance(x, float) or isinstance(y, float):
                if abs(float(x) - float(y)) > 0.01:
                    return False
            elif x != y:
                return False
    return True


def bench_inproc(run, qs, oracle, repeats):
    out = []
    for label, sql, kinds in qs:
        rows = conv(run(sql), kinds)  # first run = warm-up + result
        times = []
        for _ in range(repeats):
            t = time.perf_counter()
            run(sql)
            times.append((time.perf_counter() - t) * 1000)
        out.append((label, statistics.median(times), len(rows), same(rows, oracle[label.split()[0]])))
    return out


# ---------- SQLite ----------

def run_sqlite(a, data, out, P, oracle):
    path = out / "bench.sqlite"
    path.unlink(missing_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA journal_mode=MEMORY")
    for s in ddl("TEXT"):
        con.execute(s)
    t = time.perf_counter()
    with open(data / "jobs.csv", newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r)
        con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)", r)
    with open(data / "skills.csv", newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r)
        con.executemany("INSERT INTO job_skills VALUES (?,?)", r)
    con.commit()
    load_s = time.perf_counter() - t
    t = time.perf_counter()
    for s in INDEXES:
        con.execute(s)
    con.execute("ANALYZE")
    con.commit()
    index_s = time.perf_counter() - t
    size_mb = os.path.getsize(path) / 1048576
    qs = queries(P, "LIKE")
    res = bench_inproc(lambda sql: con.execute(sql).fetchall(), qs, oracle, a.repeats)
    con.close()
    return {"load": load_s, "index": index_s, "size_mb": size_mb, "queries": res, "file": path}


# ---------- DuckDB ----------

def run_duckdb(a, data, out, P, oracle):
    import duckdb
    path = out / "bench.duckdb"
    path.unlink(missing_ok=True)
    Path(str(path) + ".wal").unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    for s in ddl("DATE"):
        con.execute(s)
    t = time.perf_counter()
    con.execute("COPY jobs FROM '%s' (FORMAT csv, HEADER true)" % (data / "jobs.csv").as_posix())
    con.execute("COPY job_skills FROM '%s' (FORMAT csv, HEADER true)" % (data / "skills.csv").as_posix())
    load_s = time.perf_counter() - t
    t = time.perf_counter()
    con.execute(INDEXES[0])  # only the id index; other columns rely on zone maps
    con.execute("CHECKPOINT")
    index_s = time.perf_counter() - t
    size_mb = os.path.getsize(path) / 1048576
    qs = queries(P, "ILIKE")
    res = bench_inproc(lambda sql: con.execute(sql).fetchall(), qs, oracle, a.repeats)
    con.close()
    return {"load": load_s, "index": index_s, "size_mb": size_mb, "queries": res, "file": path}


# ---------- PostgreSQL (psql CLI) ----------

class PG:
    def __init__(self, a):
        self.base = [a.psql, "-X", "-q", "-h", a.pg_host, "-p", str(a.pg_port),
                     "-U", a.pg_user, "-d", a.pg_db, "-v", "ON_ERROR_STOP=1"]

    def run(self, extra):
        r = subprocess.run(self.base + extra, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "psql failed")
        return r.stdout

    def rows(self, sql):
        out = self.run(["-A", "-t", "-F", "|", "-c", sql])
        return [ln.split("|") for ln in out.splitlines() if ln.strip()]

    def script(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
            f.write(text)
            name = f.name
        try:
            return self.run(["-f", name])
        finally:
            os.unlink(name)

    def timings(self, sql, repeats):
        out = self.script("\\timing on\n" + (sql + ";\n") * (repeats + 1))
        t = [float(x.replace(",", ".")) for x in re.findall(r"^Time:\s*([\d.,]+) ms", out, re.M)]
        if len(t) < repeats + 1:
            raise RuntimeError("could not read psql timing lines")
        return t[1:]  # drop warm-up


def run_pg(a, data, out, P, oracle):
    if shutil.which(a.psql) is None and not Path(a.psql).exists():
        raise RuntimeError("psql not found; use --psql with the full path to psql.exe")
    pg = PG(a)
    try:
        ver = pg.rows("SELECT version()")[0][0]
    except Exception as ex:
        raise RuntimeError("cannot connect (check PGPASSWORD, --pg-user, --pg-db, --pg-port): %s" % ex)
    print("[pg] connected: %s" % ver[:40])
    t = time.perf_counter()
    pg.script(
        "DROP TABLE IF EXISTS job_skills;\nDROP TABLE IF EXISTS jobs;\n"
        + ";\n".join(ddl("DATE")) + ";\n"
        + "\\copy jobs FROM '%s' WITH (FORMAT csv, HEADER true)\n" % (data / "jobs.csv").as_posix()
        + "\\copy job_skills FROM '%s' WITH (FORMAT csv, HEADER true)\n" % (data / "skills.csv").as_posix())
    load_s = time.perf_counter() - t
    t = time.perf_counter()
    pg.script(";\n".join(INDEXES) + ";\nVACUUM (ANALYZE);\n")
    index_s = time.perf_counter() - t
    size = int(pg.rows("SELECT pg_total_relation_size('jobs') + pg_total_relation_size('job_skills')")[0][0])
    qs = queries(P, "ILIKE")
    res = []

    def go(label, sql, kinds):
        rows = conv(pg.rows(sql), kinds)
        ms = statistics.median(pg.timings(sql, a.repeats))
        res.append((label, ms, len(rows), same(rows, oracle[label.split()[0]])))

    for label, sql, kinds in qs:
        go(label, sql, kinds)
    try:  # optional trigram index for Q7
        pg.script("CREATE EXTENSION IF NOT EXISTS pg_trgm;\n"
                  "CREATE INDEX ix_desc_trgm ON jobs USING gin (description gin_trgm_ops);\n"
                  "VACUUM (ANALYZE) jobs;\n")
        label, sql, kinds = qs[6]
        go("Q7 text search (trgm)", sql, kinds)
    except Exception as ex:
        print("[pg] trigram index skipped: %s" % str(ex)[:120])
    return {"load": load_s, "index": index_s, "size_mb": size / 1048576, "queries": res, "file": None}


# ---------- reports ----------

def show(name, r):
    print("\n[%s] load %.2fs, index %.2fs, size %.1f MB" % (name, r["load"], r["index"], r["size_mb"]))
    print("[%s] query median ms (rows) | matches oracle" % name)
    for label, ms, nrows, ok in r["queries"]:
        print("  %-24s %9.2f [%5d]   %s" % (label, ms, nrows, "yes" if ok else "NO"))


def write_reports(out, a, res):
    with open(out / "bench_queries.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["n", "engine", "query", "median_ms", "rows", "matches_oracle"])
        for e, r in res.items():
            for label, ms, nrows, ok in r["queries"]:
                w.writerow([a.n, e, label, "%.3f" % ms, nrows, "yes" if ok else "no"])
    with open(out / "bench_loads.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["n", "engine", "load_s", "index_s", "size_mb"])
        for e, r in res.items():
            w.writerow([a.n, e, "%.2f" % r["load"], "%.2f" % r["index"], "%.1f" % r["size_mb"]])
    labels = []
    for r in res.values():
        for q in r["queries"]:
            if q[0] not in labels:
                labels.append(q[0])
    engines = list(res)
    md = ["# Benchmark results", "",
          "- Records: %d jobs (synthetic), repeats per query: %d (median)" % (a.n, a.repeats),
          "- Python %s on %s, date %s" % (platform.python_version(), platform.system(), dt.date.today()),
          "- Every answer is checked against a pure-Python oracle", "",
          "## Load and size", "",
          "| Engine | Load (s) | Index (s) | Size (MB) |", "|---|---:|---:|---:|"]
    for e in engines:
        r = res[e]
        md.append("| %s | %.2f | %.2f | %.1f |" % (e, r["load"], r["index"], r["size_mb"]))
    md += ["", "## Query median time (ms)", "",
           "| Query | " + " | ".join(engines) + " |", "|---|" + "---:|" * len(engines)]
    for label in labels:
        cells = []
        for e in engines:
            hit = [q for q in res[e]["queries"] if q[0] == label]
            if not hit:
                cells.append("-")
            else:
                cells.append("%.2f%s" % (hit[0][1], "" if hit[0][3] else " (MISMATCH)"))
        md.append("| %s | %s |" % (label, " | ".join(cells)))
    md += ["", "## Notes", "",
           "- SQLite load goes through Python executemany; DuckDB and PostgreSQL use native CSV copy.",
           "- DuckDB has only the id index (zone maps for the rest); SQLite and PostgreSQL have 5 indexes.",
           "- Q7 is a substring search on raw text (full scan). PostgreSQL also gets a pg_trgm GIN index when available.",
           "- PostgreSQL timings come from psql timing output and include a local client round trip.",
           "- PostgreSQL size = tables + indexes of the two tables (the trigram index is not included).",
           "- PostgreSQL runs VACUUM (ANALYZE) after load and after the trigram index, so background autovacuum does not disturb timings."]
    (out / "BENCHMARK.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="SQLite vs DuckDB vs PostgreSQL benchmark")
    ap.add_argument("--n", type=int, default=100000)
    ap.add_argument("--repeats", type=int, default=7)
    ap.add_argument("--engines", default="sqlite,duckdb,pg")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="bench_out")
    ap.add_argument("--pg-host", default="localhost")
    ap.add_argument("--pg-port", type=int, default=5432)
    ap.add_argument("--pg-user", default="postgres")
    ap.add_argument("--pg-db", default="fanous_bench")
    ap.add_argument("--psql", default="psql")
    ap.add_argument("--keep", action="store_true", help="keep CSV and database files")
    a = ap.parse_args()

    out = Path(a.out)
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    P, oracle = make_data(a.n, a.seed, data)
    print("generated %d jobs in %.1fs" % (a.n, time.perf_counter() - t))
    print("params:", P)

    runners = {"sqlite": run_sqlite, "duckdb": run_duckdb, "pg": run_pg}
    res = {}
    for e in [x.strip() for x in a.engines.split(",") if x.strip()]:
        try:
            res[e] = runners[e](a, data, out, P, oracle)
            show(e, res[e])
        except Exception as ex:
            print("\n[%s] skipped: %s" % (e, ex))
    if res:
        write_reports(out, a, res)
        print("\nwrote %s, %s, %s" % (out / "bench_queries.csv", out / "bench_loads.csv", out / "BENCHMARK.md"))
    if not a.keep:
        shutil.rmtree(data, ignore_errors=True)
        for r in res.values():
            if r.get("file"):
                Path(r["file"]).unlink(missing_ok=True)
        for p in out.glob("bench.duckdb*"):
            p.unlink(missing_ok=True)
    print("done")


if __name__ == "__main__":
    sys.exit(main())
