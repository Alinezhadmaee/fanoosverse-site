# Assumptions and Limitations

This document records the technical assumptions and scope boundaries of the
Fanos normalizer for Iranian job-market data.

---

## 1. Language and notation

1. **Default currency.** In Persian job ads, a numeric salary without an
   explicit unit is interpreted as toman and multiplied by 10 to produce IRR
   in `salary_min` and `salary_max`. The multiplication is skipped when the
   text explicitly says rial.
2. **Character unification.** Arabic `U+064A` and `U+0643` are converted to
   Persian `U+06CC` and `U+06A9`; tatweel (kashida) is removed; ZWNJ and
   spacing variants are normalized.
3. **Output language.** Canonical vocabulary values, skills, tools, and
   categories are emitted in English (`Title Case` or the official brand
   spelling, such as `PyTorch` and `Docker`). Provenance fields and the ad
   `text` are copied unchanged.
4. **Composite values.** Slash and conjunction separators split composite
   values, except for a whitelist of terms such as `CI/CD`, `ML/AI`,
   `TCP/IP`, `A/B Testing`, and vocabulary entries such as
   `BSc/MSc`.
5. **Placeholders.** Empty markers such as `-`, `—`, `N/A`, and the Persian
   words for "unknown" and "not available" are treated as missing values, not
   as literal strings.

---

## 2. Calendar and date conversion

1. **Two-digit Jalali years.** Dates such as `03/05/14` and `1403/05/14` are
   expanded to full years. Two-digit years up to `50` are assumed to belong
   to the 15th solar century (1400 and later).
2. **33-year arithmetic cycle.** Jalali leap years are computed with the
   arithmetic 33-year cycle. The conversion is deterministic and does not
   depend on an external calendar library.
3. **Date fields.** `source_date` and `collected_at` are emitted as ISO 8601
   Gregorian dates. `collected_at` is required by `JobAd`.

---

## 3. Privacy, security, and storage

1. **No full text in ordinary logs.** The `text` field and any token-bearing
   values are not logged at `INFO`. Log records contain identifiers, counts,
   latency, and cost, not ad content.
2. **SQLite cache validity.** The cache key includes a hash of the raw input,
   the vocabulary version, the prompt version, and the LLM mode. Changing a
   vocabulary or prompt invalidates old cache entries and forces
   re-normalization.
3. **Secrets.** API keys are read only from environment variables. They are
   never written to YAML, logs, cache keys, or audit entries.
4. **Review is not automatic approval.** A `needs_review` record is not a
   normalized record. Downstream stages must not vectorize or index it until a
   human or a later rule resolves the flagged items.

---

## 4. Scope boundaries

- The normalizer does not scrape, extract, or validate the source ad text.
  Those are upstream responsibilities.
- The normalizer does not create embeddings or write to Qdrant.
- The normalizer does not decide whether an ad is relevant; it only
  canonicalizes values.
- The default sample config uses placeholder provider endpoints. Production
  deployment must supply a real `llm.yaml`, API keys, and vocabulary files.
