# Changing the Schema and Fields

The normalizer is configuration-driven. Adding or removing a field is mostly a
schema edit plus a `config/fields.yaml` entry; core pipeline code does not
change.

---

## 1. Add a field to the pipeline

For example, add `perks`:

### Step 1: Update the Pydantic models

In `src/normalizer_agent/schemas/job_ad.py`:

```python
class JobAdRaw(BaseModel):
    ...
    perks: Any = None


class JobAd(BaseModel):
    ...
    perks: list[str] = Field(default_factory=list)
```

`JobAdRaw` is permissive: it accepts whatever the extractor sends. `JobAd` is
strict: it defines the canonical type and is validated after normalization.

### Step 2: Declare the normalization behavior

In `config/fields.yaml`:

```yaml
fields:
  ...
  perks:
    kind: vocab_list
    vocab: perk
    allow_composite_split: true
```

The set of keys in `fields.yaml` must match the `JobAd` model exactly. The
startup check fails fast if a field is missing, extra, or has an incompatible
kind.

### Step 3: Add the vocabulary (if needed)

Create `config/vocab/perk.csv`:

```csv
value,category,variants
health_insurance,benefit,health insurance|medical insurance
free_lunch,benefit,free lunch|lunch
```

- `value` is the canonical output.
- `category` is optional and used for category-scoped fields such as `tools`
  or `soft_skills`.
- `variants` is a `|`-separated list of raw spellings.
- The file hash becomes the vocabulary version used in the cache key and
  audit trail.

### Step 4: Validate the configuration

```bash
normalizer validate-config -c config
```

The startup check compares `fields.yaml` with the Pydantic model and reports
every mismatch before any record is processed.

---

## 2. Supported field kinds

| Kind | Behavior |
|---|---|
| `passthrough` | Copy the value unchanged (provenance fields) |
| `vocab_single` | Resolve one value against a controlled vocabulary |
| `vocab_list` | Resolve each item of a list against a vocabulary |
| `enum_single` | Require one member of a Python enum |
| `enum_list` | Require every item to be a member of a Python enum |
| `integer` | Extract one integer from raw text |
| `integer_range_min` | Lower bound of a range; the paired field receives the upper bound |
| `integer_range_max` | Upper bound of a range; the paired field receives the lower bound |
| `money` | Parse an amount to IRR; set the paired field and `currency_field` |
| `money_currency` | Currency code written by the money rule, not by the LLM |
| `date` | ISO 8601 Gregorian date (Jalali input is converted) |
| `location` | City or place, vocabulary-backed |
| `free_text_list` | Append-only free text, such as `evidence_notes` |

Additional options:

- `vocab`: vocabulary name (CSV file without extension)
- `allow_composite_split`: split values such as `Python/Django`
- `is_skill`: mark the field as skill-like so unresolved values go to
  `candidate_new_skill`
- `category`: restrict a vocabulary lookup to one category
- `pair`: the other bound of a range or salary pair
- `currency_field`: field that receives the currency code
- `range`: allowed integer range
- `explicit_no_member`: enum member used for an explicit "no requirement"
  answer

---

## 3. Removing or renaming a field

1. Remove the field from `JobAd` and `JobAdRaw`.
2. Remove the entry from `config/fields.yaml`.
3. Remove or rename any vocabulary file it referenced.
4. Run `normalizer validate-config -c config` and `pytest`.

The startup check prevents a half-finished rename from reaching production.
