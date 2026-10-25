# Controlled vocabularies

CSV files, UTF-8, three columns:

```text
value,category,variants(|-separated)
```

- `value`: the canonical form (English; Title Case for concepts). Enum
  members come from the Python schema; the variants column maps raw phrases
  to them.
- `category`: optional tag (`tool`, `soft_skill`, `city`, ...).
- `variants`: pipe-separated alternative spellings in any language, for
  example `py|python3|python`.

Rules:

- The file name is the vocabulary name referenced from `config/fields.yaml`
  (`vocab: capability` maps to `capability.csv`).
- The vocabulary may be empty or partial: unresolved values go to the LLM and
  its proposals are flagged for review.
- The version of a vocabulary is the SHA-256 of its bytes (first 12 hex
  chars). It lands in the audit trail and the cache key, so editing a file
  invalidates cached LLM answers.
- Lookup keys are character-unified and case-folded (Arabic `U+064A` to
  Persian `U+06CC`, Arabic `U+0643` to Persian `U+06A9`, ZWNJ to space,
  Persian digits to ASCII), so variants do not need every spelling.
- Seed files here are starter sets grown from the Fanos dataset's most
  frequent values; they are expected to be replaced or extended with the real
  controlled vocabularies.
