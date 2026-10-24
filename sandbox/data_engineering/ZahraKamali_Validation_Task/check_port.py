"""
check_port.py

Helper for the data validation task.
Checks whether the network can reach the database server found in DATABASE_URL:
it tries to open a TCP connection to the PostgreSQL ports (5432 and 6543).
It prints only the kind of host and the port numbers, never the address or the
password. A timeout usually means that a firewall, VPN or proxy blocks the port.

Usage (from this folder): python check_port.py
"""

import os
import socket
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# Read the secrets (pass.env) and the non-secret settings (.env) from this folder
load_dotenv(Path(__file__).resolve().parent / "pass.env")
load_dotenv(Path(__file__).resolve().parent / ".env")

# Split the connection string into host and port
parsed = urlparse(os.environ["DATABASE_URL"])
host, port = parsed.hostname, parsed.port or 5432

# Only the KIND of host is printed, never the full address or the password
if "pooler" in host:
    kind = "pooler"
elif host.startswith("db."):
    kind = "direct (db.)"
else:
    kind = "other"
print("host kind:", kind, "| port in DATABASE_URL:", port)

# Check that the host name can be resolved (DNS) and which address types it has
try:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    print("address types found:", sorted({i[0].name for i in infos}))
except OSError as error:
    print("DNS lookup FAILED:", type(error).__name__)
    raise SystemExit(1)

# Try to open a connection to each port; 10 seconds without an answer means a timeout
for check_port in sorted({port, 5432, 6543}):
    try:
        socket.create_connection((host, check_port), timeout=10).close()
        print(check_port, "OPEN")
    except OSError as error:
        print(check_port, "FAILED:", type(error).__name__)
