"""Date parsing (R-dates).

Target form: ISO 8601 Gregorian (``YYYY-MM-DD``). Jalali (Persian solar
Hijri) dates are converted with the standard 33-year-cycle algorithm — no
external calendar dependency. Relative dates (for example, "5 days ago") may
be resolved against ``collected_at`` but the caller MUST flag them
``inferred`` and send them to review.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from normalizer_agent.text.unify import fa_digits, unify_text

_MONTHS_GREGORIAN = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_MONTHS_JALALI = {
    "فروردین": 1,
    "اردیبهشت": 2,
    "خرداد": 3,
    "تیر": 4,
    "مرداد": 5,
    "امرداد": 5,
    "شهریور": 6,
    "مهر": 7,
    "آبان": 8,
    "آذر": 9,
    "دی": 10,
    "بهمن": 11,
    "اسفند": 12,
}

_WEEKDAYS_FA = {
    "شنبه": 5,
    "یکشنبه": 6,
    "دوشنبه": 0,
    "سهشنبه": 1,
    "سه شنبه": 1,
    "چهارشنبه": 2,
    "پنجشنبه": 3,
    "پنج شنبه": 3,
    "جمعه": 4,
}

_ISO_RE = re.compile(r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})")
_SLASH_RE = re.compile(r"(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})")
_JALALI_RE = re.compile(
    r"(?P<d>\d{1,2})\s+(?P<month>فروردین|اردیبهشت|خرداد|تیر|مرداد|امرداد|"
    r"شهریور|مهر|آبان|آذر|دی|بهمن|اسفند)\s+(?P<y>\d{4})"
)
_RELATIVE_DAYS_RE = re.compile(r"(?P<n>\d+)\s*(روز|روزانه)\s*(?:پیش|قبل|ago)")
_RELATIVE_DAYS_AGO_EN = re.compile(r"(?P<n>\d+)\s*days?\s*ago", re.IGNORECASE)
_RELATIVE_WEEKS_RE = re.compile(r"(?P<n>\d+)\s*(هفته|weeks?)\s*(?:پیش|قبل|ago)?", re.IGNORECASE)
_RELATIVE_MONTHS_RE = re.compile(r"(?P<n>\d+)\s*(ماه|months?)\s*(?:پیش|قبل|ago)?", re.IGNORECASE)


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    """Convert a Jalali date to a Gregorian ``date`` (standard 33-year cycle).

    Verified anchor: 1 Farvardin 1403 AP == 2024-03-20.
    """

    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += ((jm - 7) * 30) + 186

    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        days -= 1
        gy += days // 365
        days %= 365
    gd = days + 1

    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    months = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    for gm, length in enumerate(months, start=1):
        if gd <= length:
            return date(gy, gm, gd)
        gd -= length
    raise ValueError(f"invalid jalali date: {jy - 1595}-{jm}-{jd}")  # pragma: no cover


def parse_date(
    raw: str,
    *,
    collected_at: date | None = None,
    reference_now: date | None = None,
) -> tuple[date | None, str | None]:
    """Parse ``raw`` into a Gregorian date.

    Returns ``(date, kind)`` where ``kind`` is ``explicit`` or ``inferred``
    (relative dates such as "5 days ago" resolved against ``reference_now`` /
    ``collected_at``). ``(None, None)`` when nothing parseable is found.
    """

    unified = unify_text(raw)
    unified = fa_digits(unified)

    iso = _ISO_RE.search(unified) or _SLASH_RE.search(unified)
    if iso is not None:
        try:
            return date(int(iso["y"]), int(iso["m"]), int(iso["d"])), "explicit"
        except ValueError:
            return None, None

    jalali = _JALALI_RE.search(unified)
    if jalali is not None:
        jy = int(jalali["y"])
        try:
            return jalali_to_gregorian(
                jy, int(_MONTHS_JALALI[jalali["month"]]), int(jalali["d"])
            ), "explicit"
        except (ValueError, KeyError):
            return None, None

    # English "12 March 2025" / "March 12, 2025"
    for match in re.finditer(
        r"(?:(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}))|(?:(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4}))", unified
    ):
        day = int(match.group(1) or match.group(4))
        month_name = (match.group(2) or match.group(5) or "").lower()
        year = int(match.group(3) or match.group(6))
        month = _MONTHS_GREGORIAN.get(month_name)
        if month:
            try:
                return date(year, month, day), "explicit"
            except ValueError:
                return None, None

    now = reference_now or collected_at
    if now is not None:
        for pattern in (_RELATIVE_DAYS_RE, _RELATIVE_DAYS_AGO_EN):
            rel = pattern.search(unified)
            if rel is not None:
                return now - timedelta(days=int(rel["n"])), "inferred"
        rel = _RELATIVE_WEEKS_RE.search(unified)
        if rel is not None:
            return now - timedelta(weeks=int(rel["n"])), "inferred"
        rel = _RELATIVE_MONTHS_RE.search(unified)
        if rel is not None:
            months = int(rel["n"])
            total_months = now.year * 12 + now.month - 1 - months
            return date(total_months // 12, total_months % 12 + 1, now.day), "inferred"

    return None, None
