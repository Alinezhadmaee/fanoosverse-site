"""Integer extraction for age / experience / size fields.

``0`` is meaningful ("no experience required") and distinct from ``None``
("not stated"). Raw strings such as "at least 3 years of experience" or
"age 20 to 35" are parsed into integers with the configured plausible ranges
enforced by the validator, not here.
"""

from __future__ import annotations

import re

from normalizer_agent.rules.numbers import extract_int, extract_range
from normalizer_agent.text.unify import unify_text

_YEAR_RE = re.compile(r"\d+\s*(?:سال|years?|yrs?)|سال\s*(?:تجربه|سابقه)", re.IGNORECASE)
_MONTHS_RE = re.compile(r"\d+\s*(?:ماه|months?)", re.IGNORECASE)


def extract_experience_years(raw: str) -> int | None:
    """Minimum years of experience; ``0`` for "no experience / fresh"."""

    unified = unify_text(raw)
    if re.search(
        r"بدون\s*(?:سابقه|تجربه)|بدون نیاز به سابقه|no\s+experience|fresh",
        unified,
        flags=re.IGNORECASE,
    ):
        return 0
    value = extract_int(unified, prefer="min")
    return value


def extract_age(raw: str, *, prefer: str = "min") -> int | None:
    """Age bound from text such as "age 20 to 35" or "maximum 40 years old"."""

    unified = unify_text(raw)
    if not re.search(r"سن|سال|age", unified, flags=re.IGNORECASE):
        # a bare number is still accepted (intake value is already age-shaped)
        pass
    return extract_int(unified, prefer=prefer)


def extract_integer(raw: str, *, prefer: str = "min") -> int | None:
    """Generic first/range-aware integer extraction."""

    return extract_int(raw, prefer=prefer)


def extract_integer_range(raw: str) -> tuple[int | None, int | None]:
    """Explicit range in the raw text -> (min, max)."""

    return extract_range(raw)


def extract_months(raw: str) -> int | None:
    """Duration in months (internship_duration_months)."""

    unified = unify_text(raw)
    match = _MONTHS_RE.search(unified)
    if match is not None:
        return extract_int(match.group(0))
    # bare number on a month-kind field: treat as months
    return extract_int(unified)
