"""
test_connection.py

Helper for the data validation task (read-only).
Connects to the PostgreSQL database with DATABASE_URL and prints the names of
the tables in the "public" schema. Use it to check that the connection works
and to find the table name for DB_TABLE.

Usage (from this folder): python test_connection.py
"""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# Read the secrets (pass.env) and the non-secret settings (.env) from this folder
load_dotenv(Path(__file__).resolve().parent / "pass.env")
load_dotenv(Path(__file__).resolve().parent / ".env")
url = os.environ["DATABASE_URL"]  # never print this value, it contains a password

# Connect to the database; "with" closes the connection automatically.
# connect_timeout stops the script from waiting forever if the network blocks the port.
with psycopg.connect(url, connect_timeout=10) as conn:
    with conn.cursor() as cur:
        # SELECT only reads data, it never changes anything
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        for row in cur.fetchall():  # fetchall gives all result rows
            print(row[0])  # each row is a tuple; [0] is the table name
