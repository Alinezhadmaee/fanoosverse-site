# Extraction Loop: How It Works

This document explains how raw job ads become validated records in the Fanoos extraction pipeline
(`run_extraction.py`), and how the optional AI supervisor fits into the loop.

## 1. Big picture

```
raw text ──► Intake ──► Batching ──► Extractor LLM ──► Pydantic ──► Rule checks ──► AI supervisor (optional)
 (scrapers)  number,     up to 5      text → JSON      types and     salary_min <=      JSON vs raw text
             date,       ads per                       enums         salary_max, ...    approve / reject
             dedup       request
                                         ▲                                                   │
                                         └──── problems + previous answer (retry) ◄──────────┘
                                                      max 4 rounds per ad

approved ──► processed.jsonl            still failing after the last round ──► dropped.jsonl
```

The loop is per ad. An ad that passes every check is saved immediately. An ad that fails is sent back to the
extractor together with its previous answer and the list of problems, so the model can fix exactly those problems.

## 2. Step by step

1. **Intake.** Each raw text gets an id (`job_<collector>_0001`) and a `collected_at` date, is saved to
   `raw_ads.jsonl` at once, and exact duplicates are skipped (`duplicates.txt`). The raw file is the queue,
   so a run can always resume.
2. **Batching.** Pending ads are grouped (default 5 per request) so that the estimated input and output tokens fit
   the budget. An ad is never split between two batches. Ads longer than `ad_max_tokens` are dropped as `too_long`.
3. **Extractor LLM.** Receives the extraction prompt (`extraction_prompt.md`) and the ads. Returns
   `{"items": [...]}` with one JSON record per ad, following the JSON schema generated from `schema.py`.
4. **Pydantic.** The record is validated against `JobAd` (types, enums, no extra fields).
5. **Rule checks** (`rule_problems`). Things types cannot express: `salary_min <= salary_max`, `age_min <= age_max`,
   no negative numbers, non-empty titles, non-empty `work_mode`.
6. **AI supervisor** (only with `--review`). A second LLM compares the record with the raw text and answers
   `{"is_approved": bool, "rejection_reason": str}`. A rejection becomes the problem text
   `Reviewer: ai_supervisor: <reason>`.
7. **Retry.** For every failed ad the problems and its previous answer are added to the next request
   ("Problems to fix"). Only failed ads are asked again.
8. **Final state.** After `max_rounds` (default 4) a still failing ad is saved to `dropped.jsonl` with its raw text,
   the attempt count and the last problems.

## 3. Files and outputs

| File | Role |
|---|---|
| `run_extraction.py` | The loop (`Pipeline`), command line, `build_review()` which connects the supervisor |
| `llm_config.py` | LLM profiles per role (`EXTRACTOR`, `SUPERVISOR`) and `call_llm()`, the only function that talks to an LLM |
| `supervisor_ai.py`, `supervisor_prompt.md` | AI supervisor (`ai_review()`) and its prompt |
| `extraction_prompt.md` | Prompt of the extractor |
| `schema.py` | Pydantic schema `JobAd` of one record |
| `ad_store.py` | Files on disk: raw, processed, dropped, duplicates |
| `test_loop.py` | Offline test of the loop (fake LLM, no cost) |

Output folder (default `data/`, change with `--data-dir`):
`raw_ads.jsonl`, `processed.jsonl`, `dropped.jsonl`, `duplicates.txt`, `usage.jsonl`.

## 4. LLM profiles and `.env`

Each role has its own model, provider and key. A role copies any missing setting from the extractor, but a key is
copied only when the provider URL is the same (a key is never sent to another provider).

```
LLM_EXTRACTOR_GAPGPT=<key>              # extractor (LLM_BASE_URL, LLM_MODEL, ... are optional)
SUPERVISOR_API_KEY=<key>                # supervisor
SUPERVISOR_BASE_URL=https://api.avalai.ir/v1
SUPERVISOR_MODEL=<model id>
SUPERVISOR_STRUCTURED_MODE=json_object  # or json_schema
```

Pipeline limits can be changed with `FANOOS_*` variables, for example `FANOOS_MAX_ROUNDS=3`.
Never commit `.env`.

## 5. Running

```
python llm_config.py                                     # connection self-test (expects: Passed 2 of 2)
python test_loop.py                                      # offline loop test, no API key needed
python run_extraction.py samples.jsonl --collector hst --collected-at 2026-08-30 --max-ads 3 --data-dir data_test
python run_extraction.py samples.jsonl --collector hst --collected-at 2026-08-30 --max-ads 3 --review --data-dir data_review
python run_extraction.py                                 # continue pending ads
```

Use a new `--data-dir` for each experiment: ads that are already processed or dropped are not processed again.

## 6. What happens when something goes wrong

| Situation | Behaviour |
|---|---|
| Invalid JSON, wrong enum, rule violation | Ad goes back to the extractor with the problems (counts as a round) |
| Supervisor rejects | Same: the reason goes back to the extractor |
| Ad still failing after the last round | Saved to `dropped.jsonl` (`reason: max_rounds`) |
| Ad too long | Saved to `dropped.jsonl` (`reason: too_long`) |
| Output cut off | Batch is split in two and asked again |
| Network or rate-limit error | Retried (`llm_attempts`); the supervisor retries 3 times |
| Bad key, no credit, bad request, call limit | Run stops (`FatalError`). Finished ads stay saved; run again to continue |
| Supervisor unavailable after retries | Run stops. The ad is never accepted without review and is never counted as "rejected" |

## 7. Cost counters

`usage.jsonl` gets one line per run with totals and per-role tokens (`extractor_input_tokens`,
`supervisor_output_tokens`, ...). `max_llm_calls` (default 500) limits extractor calls per run.
Supervisor calls are not counted in `llm_calls` yet; use the token counters to watch their cost.

## 8. Known limitations and next steps

- Extraction prompt and `schema.py` must be aligned (4 newer fields have no rules in the prompt; `domain` is not nullable).
- The supervisor prompt is strict (100% fidelity). Make it accept `null`, `[]` and `["unknown"]` as valid answers
  when the text gives no clear evidence, otherwise it may reject correct conservative answers.
- Human review should stay outside this loop (sample of processed ads, reviewed from a queue or UI).
- Duplicate detection is exact-text only (SHA-256).
- Dropped ads are not retried automatically.
- Next stages (normalization, second supervisor, Postgres, vector DB) are outside this document.
