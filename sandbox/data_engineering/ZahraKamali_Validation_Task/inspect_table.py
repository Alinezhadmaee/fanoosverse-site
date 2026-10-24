"""
inspect_table.py

Helper (read-only): prints the tables, and for DB_TABLE the column names, types
and row count. It never prints row values, so no real data or secrets appear.
Use it to compare the database columns with the VectorStorePayload fields.

Usage (from this folder): python inspect_table.py
"""
import os
from pathlib import Path

import psycopg
from psycopg import sql
from dotenv import load_dotenv

# Read the secrets (pass.env) and the non-secret settings (.env) from this folder
load_dotenv(Path(__file__).resolve().parent / "pass.env")
load_dotenv(Path(__file__).resolve().parent / ".env")
url = os.environ["DATABASE_URL"]  # never print this value, it contains a password
table = os.getenv("DB_TABLE")  # optional: the table to inspect

with psycopg.connect(url, connect_timeout=15) as conn:
    with conn.cursor() as cur:
        if not table:
            # No table chosen yet: list the tables so the user can pick one
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            print("Tables:", [r[0] for r in cur.fetchall()])
            print("Set DB_TABLE in .env to see its columns.")
        else:
            # Column names and data types of the chosen table (the name is passed as a parameter)
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
                (table,),
            )
            for name, dtype in cur.fetchall():
                print(f"{name:35} {dtype}")
            # Row count only; sql.Identifier quotes the table name safely
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            print("row count:", cur.fetchone()[0])
