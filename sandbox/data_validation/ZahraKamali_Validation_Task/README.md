# Data Validation Task (ZahraKamali_Validation_Task)

Part of the Fanoos data processing pipeline. The script in this folder gets
job-ad records from the database export, keeps only the fields needed by the
vector store payload, validates every record with the project's Pydantic schema,
and saves the valid records for the vectorization step.

## Workflow

```
database source -> fetch -> select fields -> normalize -> validate -> save
```

## Files

| File | Purpose |
|---|---|
| `db_validator.py` | Main script: fetching, field selection, normalization, validation, saving |
| `validated_records.jsonl` | Output: valid records, input for the vectorization step |
| `README.md` | This documentation |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for non-secret settings (copy to `.env`) |
| `pass.env.example` | Template for secrets (copy to `pass.env`) |
| `check_port.py`, `test_connection.py`, `inspect_table.py` | Optional helpers for the direct PostgreSQL connection (see below) |

Generated locally and **not committed** (listed in `.gitignore`): `rejected_records.jsonl`
(invalid records with their errors), `validation.log` (log of the last run),
`fanoos_jobs.jsonl` (raw input file), `.env`, `pass.env`.

## Setup

1. Python 3.10 or newer.
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and choose a data source (see below).
4. If you use the API or PostgreSQL, copy `pass.env.example` to `pass.env` and fill in the secret values.
   `.env` and `pass.env` are git-ignored and must never be committed.
5. Run from this folder: `python db_validator.py`

The script needs the project schema file
`sandbox/data_processing_pipelines/AhmadrezaHashemi_Datawork/Pydantic_VDB_fields.py`
to exist in the repository (run `git pull` first).

## Data source

The source is chosen from the settings, in this priority order:

1. **HTTP API**: `DB_API_URL` is set (and `DB_API_KEY` in `pass.env`).
2. **PostgreSQL**: `DATABASE_URL` (in `pass.env`) and `DB_TABLE` are set.
3. **JSONL file**: `DB_SOURCE_FILE` is set.

### HTTP API

The database lives on Supabase, a platform built on top of PostgreSQL that exposes
the tables through an HTTPS REST API. The script sends a GET request to
`DB_API_URL` with the key in two headers: `Authorization: Bearer <key>` and
`apikey: <key>` (the header Supabase expects), and a timeout of `REQUEST_TIMEOUT`
seconds. The response must be a JSON list of records. HTTP errors (401, 404, 500,
...) stop the run with a clear exception instead of producing silent bad data.

If the API returns data in pages, set `DB_API_PAGE_SIZE` (for example `1000`).
The script then requests the records page by page with a `Range` header until a
page is shorter than the page size. The key is never printed or logged.

### PostgreSQL

`DATABASE_URL` and `DB_TABLE` make the script run a read-only
`SELECT * FROM <DB_TABLE>` (optionally limited by `DB_LIMIT`) with `psycopg`.
The table name is quoted safely, and `datetime`/`Decimal` values are converted to
plain JSON types. Helpers: `check_port.py` (is the port reachable),
`test_connection.py` (login and list tables) and `inspect_table.py` (column names
and types, no row values).

A connection error such as `invalid response to SSL negotiation` or a timeout usually
means that a VPN, proxy, firewall or the network blocks PostgreSQL traffic. Try
another network or port `6543`, or use the HTTP API, which only needs normal HTTPS.

### JSONL file

`DB_SOURCE_FILE` points to a file with one JSON object per line (the export of the
database). A line that is not valid JSON is skipped and logged.

**Source of the run below:** the real run used the JSONL export of the database
(Option 3), because the direct connection to the database was not possible from the
author's network.

## Schema and selected fields

The schema is `VectorStorePayload` from
`sandbox/data_processing_pipelines/AhmadrezaHashemi_Datawork/Pydantic_VDB_fields.py`.
It is imported, not copied, so there is one single source of truth. The script builds
the list of kept fields from the schema itself (names and aliases such as
`__comment__`), so every other field of the database record is dropped, and a
schema change is followed automatically.

| Kind | Fields |
|---|---|
| Required strings | `text`, `role_normalized`, `organization` |
| List fields (default: empty list) | `seniority`, `employment_type`, `work_mode`, `skills_required`, `tools`, `skills_preferred` |
| Optional strings | `domain`, `location`, `salary_currency`, `collected_at`, `education`, `industry`, `__comment__` |
| Optional integers | `experience_years_min`, `salary_min`, `salary_max`, `internship_duration_months` |

### Schema versions

The database record schema (`JobAd`) has more fields than the payload. The payload
schema is being extended with four more fields; the database and the schema on
GitHub are not updated yet. The plan is: test with the current schema now, and after
the database is updated, replace the schema file with the new version and run the
validation again. No code change is needed for that.

## Normalization (before validation)

The database record and the payload schema use slightly different types. These
conversions are applied before validation (the validation itself stays strict):

| Field | Conversion |
|---|---|
| `education` | list of strings is joined into one string with `, `; an empty list becomes `null` |
| `organization` | `null` becomes an empty string (the payload requires a string) |
| list fields | `null` becomes an empty list |

Everything else is validated as it is. For example, a text in an integer field is not converted.

## Invalid data handling

- Each record is validated on its own. A bad record never stops the pipeline.
- A rejected record is logged (record id, field name, error message) and saved in
  `rejected_records.jsonl` with its full list of errors.
- A record that is not a JSON object is rejected too.
- At the end a summary line with total, valid and rejected counts is logged.
- If more than `MAX_REJECT_RATIO` (default 50%) of the records are rejected, a
  warning is logged, because this usually means a problem in the source or the schema.

## Output format

`validated_records.jsonl` has one JSON object per line. Null fields are removed,
aliases such as `__comment__` are kept, and Persian text is stored as readable UTF-8.
Both output files are overwritten on every run.

## Example runs

Real run on the job-ad export (`DB_SOURCE_FILE`):

```
SUMMARY | total=203 | valid=203 | rejected=0
```

All 203 records passed the current `VectorStorePayload` schema.

Test with synthetic data, to check the rejection logic:

```
SUMMARY | total=4 | valid=1 | rejected=3
```

The rejected records were: a record without `text` (missing required field), a record
with `experience_years_min = "three"` (not an integer), and a record that was not a JSON object.
