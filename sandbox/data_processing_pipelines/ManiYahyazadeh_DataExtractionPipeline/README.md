# Fanoos Data Processing Pipeline

This task is part of the Fanoos data processing pipeline.

The pipeline takes job posting data collected by web scrapers and processes it into a structured format. An LLM extracts the required fields from the job posting text, Pydantic validates the result, and an optional AI supervisor can review each record against the raw text.

## What the pipeline does

1. Receive raw job posting texts from web scrapers.
2. Number each ad (`job_<collector>_0001`), save the raw text at once and skip exact duplicates.
3. Group pending ads into small batches that fit the token budget.
4. Send each batch to the extractor LLM and get one JSON record per ad.
5. Validate every record with Pydantic and extra rule checks (for example `salary_min <= salary_max`).
6. Optionally ask the AI supervisor to compare the record with the raw text (`--review`).
7. If an ad fails any check, send it back to the extractor together with its previous answer and the list of problems (maximum 4 rounds).
8. Save approved records to `processed.jsonl`. Ads that still fail go to `dropped.jsonl`.

The raw file is the queue, so a stopped run can always be resumed. See `PIPELINE_LOOP.md` for the full loop description and error handling.

## Main files

* `run_extraction.py` - the main loop (`Pipeline`) and the command line.
* `llm_config.py` - LLM profiles (extractor, supervisor) and `call_llm()`, the only function that talks to an LLM.
* `extraction_prompt.md` - prompt of the extractor.
* `schema.py` - Pydantic schema `JobAd` of one record.
* `ad_store.py` - manages raw ads, processed ads, dropped ads and duplicates.
* `supervisor_ai.py` and `supervisor_prompt.md` - optional AI supervisor and its prompt.
* `PIPELINE_LOOP.md` - detailed description of the extraction loop.

## Setup

Python packages: `pydantic`, `openai`, `python-dotenv`.

Create a `.env` file in this folder (it is ignored by git, never commit it). See `.env.example`:

```
LLM_EXTRACTOR=<api key>
LLM_BASE_URL=https://api.avalai.ir/v1
LLM_MODEL=<model id>

# optional, only for --review (missing values are copied from the extractor)
SUPERVISOR_API_KEY=<api key>
SUPERVISOR_BASE_URL=<url>
SUPERVISOR_MODEL=<model id>
SUPERVISOR_STRUCTURED_MODE=json_object
```

Test the connection:

```
python llm_config.py
```

## Running

```
python run_extraction.py samples.jsonl --collector hst --collected-at 2026-08-30 --max-ads 3 --data-dir data_test
python run_extraction.py samples.jsonl --collector hst --collected-at 2026-08-30 --max-ads 3 --review --data-dir data_review
python run_extraction.py        # continue pending ads
```

Use a new `--data-dir` for each experiment: ads that are already processed or dropped are not processed again.

## Input

A JSONL file. Each line is either a JSON string or an object with a `text` key. The pipeline adds the other fields itself:

```json
{"text": "Job posting text..."}
```

Each stored ad gets `document_id` (for example `job_hst_0001`), `collector`, `collected_at` and `content_hash`.

## Output

The output folder (default `data/`, change with `--data-dir`) contains:

* `raw_ads.jsonl` - every ad as received (the queue).
* `processed.jsonl` - validated records with the fields defined in `schema.py`.
* `dropped.jsonl` - ads that could not be processed, with the raw text, the reason, the number of attempts and the last problems.
* `duplicates.txt` - exact duplicate ads that were skipped.
* `usage.jsonl` - one line per run with LLM calls and token counts.

## Known limitations

* The supervisor prompt is an early version and is too strict: it asks for guesses (for example work mode or seniority) that the extraction prompt forbids, so ads can be dropped without converging. It needs to be improved before use at scale.
* Salary is stored as written (for example Toman); the currency label is not normalized yet.
* Duplicate detection is exact-text only (SHA-256).
* Dropped ads are not retried automatically.

## Notes

API keys and other private settings must stay in `.env` or environment variables and must never be committed.