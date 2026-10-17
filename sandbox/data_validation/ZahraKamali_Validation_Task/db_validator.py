"""
db_validator.py

Task: data extraction and validation with a Pydantic schema (Fanoos project).

What this module does:
    1. Fetches raw job-ad records from the database source (HTTP API, direct
       PostgreSQL connection, or a JSONL file).
    2. Keeps only the fields that are stored in the vector DB payload.
    3. Normalizes a few known type differences between the database and the payload schema.
    4. Validates every record with the project's schema (VectorStorePayload).
    5. Saves valid records for the vectorization step and keeps rejected records for review.

Workflow:  source -> fetch -> select fields -> normalize -> validate -> save

Outputs (written next to this script):
    validated_records.jsonl  - valid records, one JSON object per line
    rejected_records.jsonl   - invalid records with the validation errors
    validation.log           - full log of the run

All settings (API URL, API key, DATABASE_URL, table name, file paths) are read
from the .env file.
"""

import json
import logging
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Import the project's schema (written by another team member, so we reuse it
# instead of copying it: this keeps one single source of truth).
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = (
    BASE_DIR.parents[1] / "data_processing_pipelines" / "AhmadrezaHashemi_Datawork"
)
sys.path.insert(0, str(SCHEMA_DIR))
from Pydantic_VDB_fields import VectorStorePayload  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration (from the .env file, never hardcoded)
# ---------------------------------------------------------------------------
load_dotenv(BASE_DIR / "pass.env")  # secrets (API key, DATABASE_URL), never committed
load_dotenv(BASE_DIR / ".env")  # non-secret settings

API_URL = os.getenv("DB_API_URL")  # set this to read from an HTTP API
API_KEY = os.getenv("DB_API_KEY")  # optional, sent as a Bearer token
SOURCE_FILE = os.getenv("DB_SOURCE_FILE")  # set this to read from a JSONL file instead
DATABASE_URL = os.getenv("DATABASE_URL")  # PostgreSQL connection string (never printed)
DB_TABLE = os.getenv("DB_TABLE")  # table to read when DATABASE_URL is used
DB_LIMIT = os.getenv("DB_LIMIT")  # optional: max number of rows to read
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "15"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
API_PAGE_SIZE = os.getenv(
    "DB_API_PAGE_SIZE"
)  # set it if the API returns data in pages (e.g. Supabase REST)
# If more than this share of the records is rejected, the run prints a warning.
MAX_REJECT_RATIO = float(os.getenv("MAX_REJECT_RATIO", "0.5"))

VALIDATED_PATH = BASE_DIR / "validated_records.jsonl"
REJECTED_PATH = BASE_DIR / "rejected_records.jsonl"
LOG_PATH = BASE_DIR / "validation.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger("fanoos.validator")


# ---------------------------------------------------------------------------
# Step 1: fetch
# ---------------------------------------------------------------------------
def fetch_from_api() -> list[dict]:
    """
    Get the raw records from the HTTP API and return them as a list of dicts.
    The key is sent as a Bearer token and also as an "apikey" header (the header
    Supabase REST expects). It is never printed or logged.
    If DB_API_PAGE_SIZE is set, records are requested page by page with the
    "Range" header until a page is shorter than the page size.
    """
    headers = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
        headers["apikey"] = API_KEY

    def get_list(extra_headers: dict) -> list:
        # The timeout stops the script from waiting forever if the server is slow.
        response = requests.get(
            API_URL, headers={**headers, **extra_headers}, timeout=REQUEST_TIMEOUT
        )
        # Turn HTTP errors (401, 404, 500, ...) into exceptions instead of silent bad data.
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("The API response must be a JSON list of records.")
        return data

    if not API_PAGE_SIZE:
        return get_list({})

    page_size = int(API_PAGE_SIZE)
    records, start = [], 0
    while True:
        page = get_list(
            {"Range-Unit": "items", "Range": f"{start}-{start + page_size - 1}"}
        )
        records.extend(page)
        log.info("Fetched %d record(s) so far", len(records))
        if len(page) < page_size:
            return records
        start += page_size


def fetch_from_file(path: str) -> list[dict]:
    """Read records from a JSONL file (one JSON object per line)."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue  # ignore empty lines
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # One broken line must not stop the whole run.
                log.warning(
                    "Line %d in %s is not valid JSON, skipped", line_number, path
                )
    return records


def _json_safe(value):
    """Convert database-only types (datetime, Decimal) to plain JSON types."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def fetch_from_postgres() -> list[dict]:
    """
    Read all rows of DB_TABLE directly from PostgreSQL (read-only SELECT).
    The table name is quoted safely, and the connection string is never logged.
    """
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    query = sql.SQL("SELECT * FROM {}").format(sql.Identifier(DB_TABLE))
    if DB_LIMIT:
        query += sql.SQL(" LIMIT {}").format(sql.Literal(int(DB_LIMIT)))

    with psycopg.connect(
        DATABASE_URL, connect_timeout=DB_CONNECT_TIMEOUT, row_factory=dict_row
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    return [{k: _json_safe(v) for k, v in row.items()} for row in rows]


def fetch_records() -> list[dict]:
    """Choose the data source from the .env settings."""
    if API_URL:
        log.info("Fetching records from the API")
        return fetch_from_api()
    if DATABASE_URL and DB_TABLE:
        log.info("Reading records from PostgreSQL table: %s", DB_TABLE)
        return fetch_from_postgres()
    if SOURCE_FILE:
        log.info("Reading records from file: %s", SOURCE_FILE)
        return fetch_from_file(SOURCE_FILE)
    raise SystemExit(
        "Set DB_API_URL, or DATABASE_URL + DB_TABLE, or DB_SOURCE_FILE in the .env file."
    )


# ---------------------------------------------------------------------------
# Step 2: select the payload fields
# ---------------------------------------------------------------------------
def allowed_keys() -> set[str]:
    """
    Build the set of field names we keep. It is read from the schema itself,
    so if the schema changes, this step follows it automatically.
    Aliases are included too (for example "__comment__").
    """
    keys = set()
    for name, field in VectorStorePayload.model_fields.items():
        keys.add(name)
        if field.alias:
            keys.add(field.alias)
    return keys


def select_fields(record: dict, keys: set[str]) -> dict:
    """Keep only the keys that the vector DB payload needs."""
    return {key: value for key, value in record.items() if key in keys}


# ---------------------------------------------------------------------------
# Step 3: normalize known type differences
# ---------------------------------------------------------------------------
def normalize(record: dict) -> dict:
    """
    The database and the payload schema do not use exactly the same types.
    These conversions are applied BEFORE validation (the validation itself
    stays strict):
        - education: a list of strings becomes one string, an empty list becomes None
        - organization: None becomes an empty string (the schema requires a string)
        - list fields (skills, tools, ...): None becomes an empty list
    """
    result = dict(record)

    education = result.get("education")
    if isinstance(education, list):
        result["education"] = ", ".join(education) if education else None

    if result.get("organization") is None and "organization" in result:
        result["organization"] = ""

    for name, field in VectorStorePayload.model_fields.items():
        # Fields declared with default_factory=list are the list fields.
        if field.default_factory is list and name in result and result[name] is None:
            result[name] = []

    return result


# ---------------------------------------------------------------------------
# Step 4: validate
# ---------------------------------------------------------------------------
def validate_records(records: list) -> tuple[list[VectorStorePayload], list[dict]]:
    """
    Validate every record on its own. A bad record is logged and stored in the
    rejected list; it never stops the pipeline.
    """
    keys = allowed_keys()
    valid, rejected = [], []

    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            log.warning("Record #%d rejected: it is not a JSON object", index)
            rejected.append(
                {
                    "document_id": f"record_{index}",
                    "errors": [
                        {
                            "field": "",
                            "message": "not a JSON object",
                            "type": "type_error",
                        }
                    ],
                    "record": raw,
                }
            )
            continue

        # document_id is not part of the payload, so we read it from the raw record for logging.
        document_id = raw.get("document_id", f"record_{index}")
        selected = normalize(select_fields(raw, keys))

        try:
            payload = VectorStorePayload.model_validate(selected)
        except ValidationError as error:
            log.warning(
                "Record %s rejected (%d error(s))", document_id, error.error_count()
            )
            problems = []
            for item in error.errors():
                field_path = ".".join(str(part) for part in item["loc"])
                log.warning("    field=%s | %s", field_path, item["msg"])
                problems.append(
                    {"field": field_path, "message": item["msg"], "type": item["type"]}
                )
            rejected.append(
                {"document_id": document_id, "errors": problems, "record": selected}
            )
            continue

        valid.append(payload)

    return valid, rejected


# ---------------------------------------------------------------------------
# Step 5: save
# ---------------------------------------------------------------------------
def write_jsonl(path: Path, lines: list[str]) -> None:
    """Write one JSON string per line (UTF-8, so Persian text stays readable)."""
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def save_results(valid: list[VectorStorePayload], rejected: list[dict]) -> None:
    # exclude_none drops null fields and by_alias keeps names like "__comment__",
    # the same export settings used in the schema's own example.
    write_jsonl(
        VALIDATED_PATH,
        [p.model_dump_json(exclude_none=True, by_alias=True) for p in valid],
    )
    write_jsonl(
        REJECTED_PATH,
        [json.dumps(r, ensure_ascii=False, default=str) for r in rejected],
    )


def main() -> int:
    records = fetch_records()
    total = len(records)
    log.info("Fetched %d record(s)", total)
    if total == 0:
        log.error("No records to validate.")
        return 1

    valid, rejected = validate_records(records)
    save_results(valid, rejected)

    log.info(
        "SUMMARY | total=%d | valid=%d | rejected=%d", total, len(valid), len(rejected)
    )
    if len(rejected) / total > MAX_REJECT_RATIO:
        log.warning(
            "More than %.0f%% of the records were rejected. Check the data source or the schema.",
            MAX_REJECT_RATIO * 100,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
