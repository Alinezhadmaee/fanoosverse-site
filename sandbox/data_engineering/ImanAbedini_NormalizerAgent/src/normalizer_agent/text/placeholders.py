"""Placeholder and explicit-no detection (R-placeholder, R-explicit-no).

Placeholders ("unknown", "N/A", "-", ...) are never emitted as values.
Explicit-no ("not important", "does not matter") is a real value: it maps to
the enum's "any"-style member when the field has one.
"""

from __future__ import annotations

from normalizer_agent.text.unify import unify_key

_PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "?",
        "؟",
        "؟؟",
        "??",
        "???",
        "!",
        ".",
        "[]",
        "[ ]",
        "[not mentioned]",
        "n/a",
        "na",
        "n.a.",
        "none",
        "null",
        "nil",
        "unknown",
        "unspecified",
        "not specified",
        "not mentioned",
        "not stated",
        "not applicable",
        "not provided",
        "no mention",
        "nan",
        "نامشخص",
        "نامعلوم",
        "ذکر نشده",
        "اشاره نشده",
        "تعیین نشده",
        "مشخص نشده",
        "مشخص نیست",
        "قید نشده",
        "ندارد",
        "ندارذ",
    }
)

_EXPLICIT_NO = frozenset(
    {
        "مهم نیست",
        "مهمنیست",
        "فرقی ندارد",
        "تفاوتی ندارد",
        "فرقی نمیکند",
        "لااهمیت ندارد",
        "not important",
        "no requirement",
        "no preference",
        "does not matter",
        "doesn't matter",
        "doesn't have importance",
        "irrelevant",
        "any",
    }
)


def is_placeholder(value: str) -> bool:
    """True when ``value`` carries no information and must not be emitted."""

    return unify_key(value) in _PLACEHOLDERS


def is_explicit_no(value: str) -> bool:
    """True when ``value`` explicitly says "no requirement / doesn't matter"."""

    return unify_key(value) in _EXPLICIT_NO
