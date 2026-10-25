"""Number extraction from raw (mostly Persian) text.

Supports ASCII digits with separators, Persian number words ("thirty five"),
range patterns ("20 to 35"), and magnitude multipliers for money.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from normalizer_agent.text.unify import unify_text

_UNITS = {
    "صفر": 0,
    "یک": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6,
    "شیش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
    "ده": 10,
    "یازده": 11,
    "دوازده": 12,
    "سیزده": 13,
    "چهارده": 14,
    "پانزده": 15,
    "پونزده": 15,
    "شانزده": 16,
    "هفده": 17,
    "هجده": 18,
    "نوزده": 19,
    "بیست": 20,
    "سی": 30,
    "چهل": 40,
    "پنجاه": 50,
    "شصت": 60,
    "هفتاد": 70,
    "هشتاد": 80,
    "نود": 90,
    "صد": 100,
    "دویست": 200,
    "سیصد": 300,
    "چهارصد": 400,
    "پانصد": 500,
    "ششصد": 600,
    "هفتصد": 700,
    "هشتصد": 800,
    "نهصد": 900,
}

_NUMBER_WORD_RE = re.compile(
    r"\b(?:صفر|یک|دو|سه|چهار|پنج|شش|شیش|هفت|هشت|نه|ده|یازده|دوازده|سیزده|"
    r"چهارده|پانزده|پونزده|شانزده|هفده|هجده|نوزده|بیست|سی|چهل|پنجاه|شصت|"
    r"هفتاد|هشتاد|نود|صد|دویست|سیصد|چهارصد|پانصد|ششصد|هفتصد|هشتصد|نهصد)"
    r"(?:\s+و\s+(?:یک|دو|سه|چهار|پنج|شش|شیش|هفت|هشت|نه|ده|یازده|دوازده|سیزده|"
    r"چهارده|پانزده|پونزده|شانزده|هفده|هجده|نوزده|بیست|سی|چهل|پنجاه|شصت|"
    r"هفتاد|هشتاد|نود|صد|دویست|سیصد|چهارصد|پانصد|ششصد|هفتصد|هشتصد|نهصد))*\b"
)

_ASCII_NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")

_RANGE_RE = re.compile(
    r"(?:از\s+)?(?P<low>\d[\d.,]*|[\u0600-\u06FF]+(?:\s+و\s+[\u0600-\u06FF]+)*)"
    r"\s*(?:تا|الی|until|-|–|—|to)\s*"
    r"(?P<high>\d[\d.,]*|[\u0600-\u06FF]+(?:\s+و\s+[\u0600-\u06FF]+)*)"
)


@dataclass(slots=True)
class NumberResult:
    value: int | None = None
    range_min: int | None = None
    range_max: int | None = None


def _words_to_int(phrase: str) -> int | None:
    """Compose a Persian number-word phrase ("thirty five") into an int.

    Standard Persian numbers below 1000 are purely additive:
    "two hundred and thirty five" = 200 + 30 + 5.
    """

    total = 0
    matched = False
    for token in phrase.split():
        if token == "و":
            continue
        value = _UNITS.get(token)
        if value is None:
            return None
        total += value
        matched = True
    return total if matched else None


def parse_ascii_number(token: str) -> int | float | None:
    cleaned = token.replace(",", "")
    if not cleaned.isdigit():
        try:
            return float(cleaned)
        except ValueError:
            return None
    return int(cleaned)


def find_numbers(text: str) -> list[int | float]:
    """All numbers appearing in ``text`` (ASCII digits first, then words)."""

    unified = unify_text(text)
    raw_parsed = [parse_ascii_number(match) for match in _ASCII_NUMBER_RE.findall(unified)]
    found: list[int | float] = [v for v in raw_parsed if v is not None]
    for match in _NUMBER_WORD_RE.finditer(unified):
        value = _words_to_int(match.group(0))
        if value is not None:
            found.append(value)
    return found


def extract_int(text: str, *, prefer: str = "min") -> int | None:
    """First integer in ``text``; ranges resolved per ``prefer`` ('min'|'max')."""

    numbers = [int(n) for n in find_numbers(text) if float(n).is_integer()]
    if not numbers:
        return None
    if len(numbers) >= 2 and _RANGE_RE.search(unify_text(text)):
        return min(numbers) if prefer == "min" else max(numbers)
    return numbers[0]


def extract_range(text: str) -> tuple[int | None, int | None]:
    """Explicit A-to-B range in ``text`` -> (min, max); (None, None) if absent."""

    match = _RANGE_RE.search(unify_text(text))
    if match is None:
        [int(n) for n in find_numbers(text) if float(n).is_integer()]
        return (None, None)
    low_raw, high_raw = match.group("low"), match.group("high")
    low = parse_ascii_number(low_raw) if low_raw[:1].isdigit() else _words_to_int(low_raw)
    high = parse_ascii_number(high_raw) if high_raw[:1].isdigit() else _words_to_int(high_raw)
    if low is None or high is None:
        return (None, None)
    return (int(low), int(high))
