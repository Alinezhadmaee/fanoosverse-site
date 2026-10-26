# Fanos Normalizer Agent — Architecture

This document describes the technical architecture, data flow, and design
decisions of the Normalizer Agent in the Fanos Iranian job-market
intelligence pipeline.

---

## 1. Purpose and design philosophy

The normalizer receives raw records extracted from Iranian job advertisements
(for example Jobinja, Karboom, IranTalent, JobVision, and Quera) and turns
every field value into the project's canonical representation.

The core design principles are:

1. **Deterministic first.** Character unification, number parsing, Jalali
   date conversion, salary unit conversion, and common phrase handling run
   before any LLM call and cost nothing. The LLM is a fallback for values
   that rules and vocabularies cannot resolve.
2. **Never guess.** When a value cannot be mapped confidently, the system
   does not invent a canonical value. It leaves the field empty, flags the
   record as `needs_review`, and records a machine-readable reason.
3. **Provider agnostic.** Changing the model or provider is a
   `config/llm.yaml` edit. No vendor name, SDK, or model identifier appears
   outside `llm/providers/`.
4. **Provenance is immutable.** `document_id`, `collector`, `source_type`,
   `text`, `content_hash`, `source_date`, `collected_at`, and `version` are
   copied through unchanged.
5. **Full auditability.** Every emitted value records its raw input,
   canonical output, resolution method (`rule` / `lookup` / `llm` /
   `cache`), confidence, provider, model, and vocabulary version.

---

## 2. Record pipeline

Each record goes through six stages:

```text
[Input: JobAdRaw]
        |
        v
[Stage 1: Text unification, placeholder removal, parenthesis handling]
        |
        v
[Stage 2: Deterministic rules + vocabulary lookup]
        |
   +----+---------------------------+
   |                                |
[All fields resolved?]        [Unresolved fields remain]
   | yes                            |
   |                                v
   |                     [Stage 3: SQLite cache lookup]
   |                                |
   |                       +--------+--------+
   |                  [Cache hit]       [Cache miss]
   |                       |                 |
   |                       |                 v
   |                       |      [Stage 4: Batched LLM call]
   |                       |       - one request per record
   |                       |       - fallback router
   |                       |       - JSON repair loop
   |                       |                 |
   |                       |                 v
   |                       |      [Stage 5: Deterministic validation]
   |                       +--------+--------+
   |                                |
   +----------------+---------------+
                    v
   [Stage 6: Envelope assembly (JobAd + audit + review items)]
                    |
                    v
   [Output: NormalizeResult]
```

A failed record never stops a batch: every failure is captured in its own
envelope with `status`, `error_codes`, `review_items`, `audit`, `usage`, and
`versions`.

---

## 3. Packages

The code lives under `src/normalizer_agent/`:

| Package | Responsibility |
|---|---|
| `schemas/` | Pydantic v2 models: `JobAdRaw`, `JobAd`, `NormalizeRequest`, `NormalizeResult`, and field enums |
| `text/` | Unicode unification (Arabic `U+064A`/`U+0643` to Persian `U+06CC`/`U+06A9`), digit conversion, placeholder detection, parenthesis extraction, composite splitting |
| `rules/` | Money parsing (toman to IRR), Jalali-to-Gregorian dates, Persian number words, integer ranges for age, experience, and company size |
| `vocab/` | CSV-backed controlled vocabularies, SHA-256 versions, and a trigram/Jaccard fuzzy retriever |
| `llm/` | Provider ports and adapters, `FallbackRouter`, circuit breaker, rate limiter, retry/backoff |
| `cache/` | SQLite result cache keyed by `(field_kind, raw_value, vocab_version, prompt_version, llm_mode)` |
| `validate/` | Post-LLM checks: shape, vocabulary membership, numeric consistency |
| `pipeline/` | `Collector`, `DeterministicResolver`, `LLMStage`, prompt renderer, record assembly |
| `interface/` | CLI, Python API facade, coordinator adapter, optional FastAPI service |
| `review/` | Human-review sink interface and in-memory implementation |
| `observability/` | Structured logging and pipeline metrics |

---

## 4. Security and error handling

1. **Secrets.** API keys are read from environment variables only. They are
   never written to YAML, never logged, and never included in audit entries.
2. **Logging.** Full ad `text` and token values are never logged at `INFO`.
   Log records contain identifiers and counters, not payloads.
3. **Isolation.** A validation error or malformed LLM answer affects only the
   current record. The envelope records `rejected` or `needs_review` and the
   batch continues.
4. **Review queue.** Records that cannot be normalized confidently are sent
   to a `ReviewSink` with reasons such as `not_in_vocabulary`,
   `low_confidence`, `inferred`, or `validation_failed`.
