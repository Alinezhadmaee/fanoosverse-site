"""Character unification applied before any matching (R-chars).

Arabic letter variants collapse to Persian, Persian/Arabic-Indic digits to
ASCII, ZWNJ becomes a single space, bidi marks and tatweel are dropped, and
whitespace is collapsed. This is the single canonical "unified" form every
lookup key and rule operates on.
"""

from __future__ import annotations

import re
import unicodedata

# Persian (U+06F0..) and Arabic-Indic (U+0660..) digits -> ASCII
_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

# Arabic letter variants -> Persian / bare forms (for matching only)
_LETTER_TRANSLATION = str.maketrans(
    {
        "\u064a": "\u06cc",  # arabic yeh -> farsi yeh
        "\u0649": "\u06cc",  # alef maksura -> farsi yeh
        "\u0643": "\u06a9",  # arabic kaf -> keheh
        "\u0623": "\u0627",  # alef with hamza -> alef
        "\u0625": "\u0627",  # alef with hamza below -> alef
        "\u0622": "\u0627",  # alef with madda -> alef
        "\u0671": "\u0627",  # alef wasla -> alef
        "\u0629": "\u0647",  # teh marbuta -> heh
        "\u0640": "",  # tatweel
        "\u064b": "",  # fathatan
        "\u064c": "",  # dammatan
        "\u064d": "",  # kasratan
        "\u064e": "",  # fatha
        "\u064f": "",  # damma
        "\u0650": "",  # kasra
        "\u0651": "",  # shadda
        "\u0652": "",  # sukun
        "\u066b": ".",  # arabic decimal separator
        "\u066c": "",  # arabic thousands separator
        "\u200c": " ",  # ZWNJ -> space (matching form)
        "\u200e": "",  # LRM
        "\u200f": "",  # RLM
        "\u061c": "",  # ALM
        "\ufeff": "",  # BOM / zero-width no-break space
    }
)

_WHITESPACE_RE = re.compile(r"\s+")


def unify_text(value: str) -> str:
    """Return the unified matching form of ``value``.

    Unicode NFC first so decomposed forms match, then the translation tables,
    then whitespace collapse.
    """

    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.translate(_DIGIT_TRANSLATION).translate(_LETTER_TRANSLATION)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def unify_key(value: str) -> str:
    """Unified + case-folded + quote-stripped key for vocabulary lookups."""

    unified = unify_text(value).casefold()
    return unified.strip(" \t\"'`").strip()


def fa_digits(value: str) -> str:
    """Translate Persian/Arabic-Indic digits to ASCII (letters untouched)."""

    return value.translate(_DIGIT_TRANSLATION)
