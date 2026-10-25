"""Post-LLM deterministic validation (task 4 checks).

Everything the LLM proposes is re-checked here; failures route items to
review or trigger a repair retry. No check mutates values silently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum

from normalizer_agent.rules.naming import is_snake_case, is_title_case
from normalizer_agent.text.composite import split_composite
from normalizer_agent.text.placeholders import is_placeholder
from normalizer_agent.text.unify import unify_key

_PAREN_RE = re.compile(r"[()（）]")


def naming_format_ok(canonical: str, *, is_enum: bool) -> bool:
    """snake_case for enum members; Title Case-ish for concepts; no junk."""

    value = canonical.strip()
    if not value or value != canonical or _PAREN_RE.search(value):
        return False
    if is_enum:
        return is_snake_case(value)
    if " " in value:
        return is_title_case(value)
    if value.isupper():
        return True  # acronym
    return value[0].isupper() or not value[0].isalpha()


def no_composite(canonical: str, whitelist: Iterable[str]) -> bool:
    """True when the value would NOT be split by the composite splitter."""

    return len(split_composite(canonical, set(unify_key(w) for w in whitelist))) == 1


def member_of_enum(value: str, enum_cls: type[Enum] | type[str]) -> bool:
    try:
        enum_cls(value)
    except (ValueError, TypeError):
        return False
    return True


def numeric_sanity(
    *,
    min_value: int | None,
    max_value: int | None,
    value_range: tuple[int, int] | None,
) -> list[str]:
    """Returns human-readable problems; empty list is healthy."""

    problems: list[str] = []
    if min_value is not None and max_value is not None and min_value > max_value:
        problems.append("min > max")
    for value in (min_value, max_value):
        if value is None:
            continue
        if value_range is not None and not (value_range[0] <= value <= value_range[1]):
            problems.append(f"value {value} outside {value_range}")
    return problems


def salary_plausibility(amount_irr: int, *, low: int, high: int) -> str | None:
    if amount_irr < low:
        return f"salary {amount_irr} IRR below {low} (implausible)"
    if amount_irr > high:
        return f"salary {amount_irr} IRR above {high} (implausible)"
    return None


def list_hygiene(values: list[str]) -> tuple[list[str], list[str]]:
    """Dedup + sort for stable output; returns (clean, problems)."""

    problems: list[str] = []
    seen: set[str] = set()
    clean: list[str] = []
    for value in values:
        key = unify_key(value)
        if key in seen:
            problems.append(f"duplicate '{value}'")
            continue
        if is_placeholder(value):
            problems.append(f"placeholder '{value}' leaked")
            continue
        seen.add(key)
        clean.append(value)
    return sorted(clean), problems
