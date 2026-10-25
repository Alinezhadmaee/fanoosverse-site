"""Tests for rules/: numbers, money, dates, integers, naming."""

from __future__ import annotations

from datetime import date

from normalizer_agent.rules.dates import jalali_to_gregorian, parse_date
from normalizer_agent.rules.integers import (
    extract_age,
    extract_experience_years,
    extract_months,
)
from normalizer_agent.rules.money import parse_money
from normalizer_agent.rules.naming import is_snake_case, title_case
from normalizer_agent.rules.numbers import extract_int, extract_range


class TestNumbers:
    def test_ascii(self) -> None:
        assert extract_int("5 سال") == 5

    def test_persian_digits(self) -> None:
        assert extract_int("۵ سال") == 5

    def test_number_words(self) -> None:
        assert extract_int("سی و پنج") == 35
        assert extract_int("دویست") == 200

    def test_range_min(self) -> None:
        assert extract_int("۲۰ تا ۳۵ سال", prefer="min") == 20
        assert extract_int("۲۰ تا ۳۵ سال", prefer="max") == 35

    def test_extract_range(self) -> None:
        assert extract_range("38 - 52") == (38, 52)
        assert extract_range("سن ۲۰ تا ۳۵ سال") == (20, 35)
        assert extract_range("بدون سابقه") == (None, None)


class TestMoney:
    def test_million_toman(self) -> None:
        result = parse_money("۳۰ میلیون تومان")
        assert result.min_irr == 300_000_000
        assert result.currency == "IRR"

    def test_range_toman(self) -> None:
        result = parse_money("۳۸ تا ۵۲ میلیون تومان")
        assert result.min_irr == 380_000_000
        assert result.max_irr == 520_000_000

    def test_ascii_range(self) -> None:
        result = parse_money("38 - 52 Million Tomans")
        assert result.min_irr == 380_000_000
        assert result.max_irr == 520_000_000

    def test_one_sided(self) -> None:
        result = parse_money("حداقل ۳۰ میلیون")
        assert result.min_irr == 300_000_000
        assert result.max_irr is None

    def test_negotiable_never_zero(self) -> None:
        result = parse_money("توافقی")
        assert result.negotiable
        assert result.min_irr is None and result.max_irr is None

    def test_foreign_not_converted(self) -> None:
        result = parse_money("3000 USD monthly")
        assert result.currency == "USD"
        assert result.amount == 3000
        assert result.min_irr is None

    def test_rial_not_multiplied(self) -> None:
        result = parse_money("۵۰۰ میلیون ریال")
        assert result.min_irr == 500_000_000

    def test_no_amount(self) -> None:
        result = parse_money("حقوق خوب")
        assert result.min_irr is None and result.note == "no_amount"


class TestDates:
    def test_jalali_anchor(self) -> None:
        assert jalali_to_gregorian(1403, 1, 1) == date(2024, 3, 20)

    def test_jalali_named_month(self) -> None:
        value, kind = parse_date("۱۵ مهر ۱۴۰۳")
        assert value == date(2024, 10, 6)
        assert kind == "explicit"

    def test_iso(self) -> None:
        value, kind = parse_date("2025-03-01")
        assert value == date(2025, 3, 1) and kind == "explicit"

    def test_slash(self) -> None:
        value, _ = parse_date("2025/03/01")
        assert value == date(2025, 3, 1)

    def test_relative_days_flagged_inferred(self) -> None:
        value, kind = parse_date("۵ روز پیش", reference_now=date(2026, 8, 29))
        assert value == date(2026, 8, 24) and kind == "inferred"

    def test_relative_days_english(self) -> None:
        value, kind = parse_date("5 days ago", reference_now=date(2026, 8, 29))
        assert value == date(2026, 8, 24) and kind == "inferred"

    def test_unparseable(self) -> None:
        assert parse_date("به زودی") == (None, None)


class TestIntegers:
    def test_experience_min(self) -> None:
        assert extract_experience_years("حداقل ۳ سال سابقه") == 3

    def test_experience_zero_is_zero(self) -> None:
        assert extract_experience_years("بدون سابقه") == 0
        assert extract_experience_years("No experience required") == 0

    def test_age_range(self) -> None:
        assert extract_age("۲۰ تا ۳۵ سال", prefer="min") == 20
        assert extract_age("۲۰ تا ۳۵ سال", prefer="max") == 35

    def test_months(self) -> None:
        assert extract_months("۶ ماه") == 6


class TestNaming:
    def test_title_case(self) -> None:
        assert title_case("machine learning") == "Machine Learning"
        assert title_case("pytorch") == "Pytorch"

    def test_acronyms(self) -> None:
        assert title_case("llm") == "LLM"
        assert title_case("nlp") == "NLP"

    def test_snake(self) -> None:
        assert is_snake_case("full_time")
        assert is_snake_case("full time") is False
