# Fanoos Web Scraping Pipeline - Architecture & Module Reference

## 1. Executive Summary & Core Objective

The **Fanoos Web Scraping Pipeline** is a production-grade, highly resilient data ingestion engine engineered specifically to scrape Iranian AI and software engineering job markets (such as *Jobinja*, *JobVision*, *IranEstekhdam*, and *Quera*). 

Its core design principles prioritize:
* **Data Integrity & Historical Preservation:** Capturing active and expired job ads without deletion for comprehensive market analysis.
* **Anti-Bot & WAF Resilience:** Impersonating real browser TLS/JA3 fingerprints via `curl_cffi` and falling back to stealth Playwright when challenged by Cloudflare or ArvanCloud.
* **RTL & Persian Normalization:** Deterministically cleaning Persian typography (unifying Arabic `ي`/`ك`, converting numerals to ASCII, preserving ZWNJs, and de-gluing tech terms).
* **Downstream Agent Compatibility:** Delivering clean, standardized `RawJobAd` JSONL contracts ready for Mani Yahyazadeh's entity-extraction agent and the Fanoos Supervisor.

---

## 2. High-Level Architecture & Flow

```text
[ url_seeder.py ] ---> ( PENDING URLs ) ---> [ state_ledger.py (SQLite Ledger) ]
                                                   |
                                                   v (Atomic Lease)
                                           [ scraper_service.py ]
                                                   |
                                                   v
                                           [ transport.py (Network) ]
                                                   |
                                                   v
                                         [ cascade_extractor.py ]
                                                   |
                                                   v
                                           [ rtl_sanitizer.py ]
                                                   |
                                                   v
                                       (preflight_gate.py) [Quality Gate]
                                                   |
                   +---------------------------------+---------------------------------+
                   |                                                                   |
           (auto_ack=True)                                                      (auto_ack=False)
                   |                                                                   |
                   v                                                                   v
         [ StateLedger: ACK ]                                               [ Supervisor Agent ACK/NACK ]
                   |
                   v
         [ output/raw_persian_jobs.jsonl ]
```

## 3. Module Breakdown & Detailed Functionality

### 1. config.py (Central Configuration & Fail-Fast Validation)
- **Purpose:** Manages global runtime settings (`.env`) and platform scraping rules (`config/sites_config.yaml`).
- **Functionality:**
  - Enforces fail-fast validation upon startup via Pydantic v2 to guarantee required network parameters, proxy strings, and filesystem paths exist before execution.
  - Exposes declarative platform configurations (selectors, rate limits, `requires_js` flags) so Python code remains 100% website-agnostic.

### 2. logger.py (Structured Dual-Sink Logging & Context Tracing)
- **Purpose:** Provides production-grade observability across all pipeline components.
- **Functionality:**
  - Implements dual-sink output: human-readable colorized console logs and daily rotating UTF-8 JSON files (`logs/scraper.json.log`).
  - Uses Python `contextvars` to bind contextual execution trace IDs (`job_id`, `platform`, `request_duration_ms`) automatically to every log record.

### 3. models.py (Strictly Typed Pydantic Lifecycle Contracts)
- **Purpose:** Defines data models for every stage of the pipeline lifecycle.
- **Functionality:**
  - Manages `LedgerJobRecord`, `TransportResponse`, `ExtractedPrimitives`, and `PreflightReport`.
  - Computes deterministic SHA-256 URL hashes and canonicalizes URLs (stripping UTM tracking parameters) to prevent duplicate crawling.
  - Defines the final `RawJobAd` handoff contract delivered to downstream agents.

### 4. state_ledger.py (ACID SQLite Discovery & Queue Manager)
- **Purpose:** Coordinates task queues, deduplication, and transactional states inside `scraper_ledger.db`.
- **Functionality:**
  - Enforces ACID guarantees using SQLite WAL mode and `BEGIN IMMEDIATE` atomic locking.
  - Bulk-registers discovered URLs via `INSERT OR IGNORE` deduplication.
  - Handles single-item leasing, automatic timeout recovery for crashed workers (`reclaim_expired_leases`), and full transactional life-cycle updates (`ACK`, `NACK`, `DEAD_LETTER`, `REJECTED_PREFLIGHT`).

### 5. transport.py (Resilient Dual-Engine Network Fetcher)
- **Purpose:** Bypasses Iranian hosting restrictions, WAFs, and anti-bot challenges.
- **Functionality:**
  - Executes primary HTTP requests using `curl_cffi` with Chrome TLS/JA3 impersonation (`chrome124`) and polite per-platform rate limiting with jitter.
  - Automatically implements a smart domestic `.ir` proxy bypass to prevent foreign IP blocks on ArvanCloud.
  - Intercepts 403/503 blocks, CAPTCHA strings, or unrendered JavaScript SPA shells (`<app-root>`, `div#__next`) and escalates to a stealth Playwright Chromium instance.

### 6. cascade_extractor.py (Resilient 3-Tier HTML Extractor)
- **Purpose:** Isolates raw Persian job content from unpredictable HTML layouts.
- **Functionality:**
  - **Step 0:** Inspects the DOM for CAPTCHA widgets (`g-recaptcha`, `cf-turnstile`) and WAF text signatures before parsing.
  - **Tier 1:** Parses structured `schema.org/JobPosting` JSON-LD blocks.
  - **Tier 2:** Applies declarative CSS/XPath selectors from `sites_config.yaml`, automatically merging top metadata info-boxes (salary, experience, skills) with the main description.
  - **Tier 3:** Terminates with Trafilatura and semantic DOM pruning heuristics.

### 7. rtl_sanitizer.py (Deterministic RTL & Persian Text Normalizer)
- **Purpose:** Standardizes raw Persian typography and technical tokens for downstream LLM agents.
- **Functionality:**
  - Strips invisible bidirectional formatting markers (LRM, RLM, BOM, Tatweel) while strictly preserving valid intra-word ZWNJs (`\u200c` / نیم‌فاصله).
  - Normalizes Arabic orthography (`ك` $\rightarrow$ `ک`, `ي`/`ى` $\rightarrow$ `ی`) and converts Persian/Arabic numerals to ASCII (`۰-۹` $\rightarrow$ `0-9`).
  - De-glues concatenated Persian prepositions and English technical keywords (`مسلط بهPythonوFastAPI` $\rightarrow$ `مسلط به Python و FastAPI`).

### 8. preflight_gate.py (Data Quality & Expiration Checkpoint)
- **Purpose:** Verifies content validity and preserves historical market data.
- **Functionality:**
  - Detects closed or expired job ad signatures ("آگهی منقضی شده", "مهلت ارسال رزومه تمام شده") and flags them with `is_expired=True` without discarding them, ensuring complete historical coverage for labor market analysis.
  - Filters out Soft-404 error pages, truncated login walls, and UTF-8 Mojibake corruption while tolerating legitimate English-language tech job posts.

### 9. url_seeder.py (Discovery & Pagination Crawler)
- **Purpose:** Crawls listing pages to discover new job advertisement URLs.
- **Functionality:**
  - Navigates pagination across platforms (supporting both WordPress `/page/N/` path structures and query-string `?page=N` parameters).
  - Extracts anchor links, validates same-domain security, filters out navigation/login links, preserves top-to-bottom chronological listing order, and registers them into `StateLedger`.

### 10. scraper_service.py (Orchestration Facade)
- **Purpose:** Ties all modules together into a unified, thread-safe service.
- **Functionality:**
  - Coordinates the end-to-end `lease` $\rightarrow$ `fetch` $\rightarrow$ `extract` $\rightarrow$ `sanitize` $\rightarrow$ `preflight` $\rightarrow$ `ACK` pipeline.
  - Handles mid-stream CAPTCHA/SPA escalation retries.
  - Supports both automated standalone ACK mode and deferred supervisor ACK mode.

### 11. main.py (CLI Entrypoint & JSONL Exporter)
- **Purpose:** Provides a command-line interface and batch exporter for running the scraper.
- **Functionality:**
  - Exposes CLI flags (`--seed`, `--limit`, `--platform`, `--status`, `--overwrite`).
  - Exports validated `RawJobAd` records to `output/raw_persian_jobs.jsonl` with built-in deduplication guards.