"""Money parsing (R-money).

Storage unit is IRR: ``1 toman = 10 rial``. Persian magnitude words multiply
("million"=1e6, "billion"=1e9, "thousand"=1e3). Foreign currencies are NOT
converted: the amount and its ISO 4217 code survive as-is. "Negotiable" is
not a number and must never become 0. The pay period is never inferred from
the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from normalizer_agent.rules.numbers import find_numbers
from normalizer_agent.text.unify import unify_text

_MAGNITUDES: list[tuple[str, int]] = [
    (r"میلیارد|billion|\bbn\b|(?<![a-z])b(?![a-z])", 1_000_000_000),
    (r"میلیون|million|\bmn\b|(?<![a-z])m(?![a-z])", 1_000_000),
    (r"هزار|thousand|\bk\b", 1_000),
]

_CURRENCIES: list[tuple[str, str]] = [
    (r"تومان|toman|tomans|\bt\b", "IRR"),
    (r"ریال|rial|rials|\birr\b", "IRR"),
    (r"دلار|dollar|dollars|usd|us\$|\$", "USD"),
    (r"یورو|euro|euros|eur|€", "EUR"),
]

_NEGOTIABLE_RE = re.compile(r"توافقی|توافق|در توافق|negotiable|upon\s+agreement|agreed")


@dataclass(slots=True)
class MoneyResult:
    min_irr: int | None = None
    max_irr: int | None = None
    currency: str = "IRR"
    amount: int | None = None  # raw amount when currency is foreign
    negotiable: bool = False
    note: str | None = None


def _magnitude(text: str) -> int:
    for pattern, factor in _MAGNITUDES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return factor
    return 1


def _currency(text: str) -> str | None:
    for pattern, code in _CURRENCIES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return code
    return None


def parse_money(text: str) -> MoneyResult:
    unified = unify_text(text)
    if _NEGOTIABLE_RE.search(unified):
        return MoneyResult(negotiable=True, note="negotiable")

    numbers = [n for n in find_numbers(unified) if n > 0]
    if not numbers:
        return MoneyResult(note="no_amount")

    currency = _currency(unified)
    magnitude = _magnitude(unified)
    low = min(numbers)
    high = max(numbers)

    if currency in {"USD", "EUR"}:
        return MoneyResult(
            amount=int(high if len(numbers) > 1 else numbers[0]),
            currency=currency,
            min_irr=None,
        )

    # IRR: Persian job ads state amounts in toman by convention, so an amount
    # with no explicit unit is treated as toman (x10 to IRR). An explicit
    # The Persian word for rial disables the toman multiplication.
    rial = re.search(r"ریال|rials?\b", unified, flags=re.IGNORECASE)
    factor = magnitude * (1 if rial else 10)
    is_range = len(numbers) > 1 and re.search(r"تا|الی|-|–|until|\bto\b", unified)
    if is_range:
        return MoneyResult(
            min_irr=int(low) * factor,
            max_irr=int(high) * factor,
            currency="IRR",
        )
    amount = int(numbers[0]) * factor
    return MoneyResult(min_irr=amount, max_irr=None, currency="IRR")
