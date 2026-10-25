"""Tests for text/: unification, placeholders, parentheses, composites."""

from __future__ import annotations

import pytest

from normalizer_agent.text.composite import split_composite
from normalizer_agent.text.parens import strip_parentheticals
from normalizer_agent.text.placeholders import is_explicit_no, is_placeholder
from normalizer_agent.text.unify import fa_digits, unify_key, unify_text


class TestUnify:
    def test_arabic_yeh_kaf(self) -> None:
        assert unify_text("ي ك") == "ی ک"

    def test_persian_digits(self) -> None:
        assert unify_text("۳۰") == "30"
        assert unify_text("٠١٢") == "012"

    def test_zwnj_becomes_space(self) -> None:
        assert unify_text("می\u200cشود") == "می شود"

    def test_whitespace_collapsed(self) -> None:
        assert unify_text("  a\t\nb  ") == "a b"

    def test_hamza_forms(self) -> None:
        assert unify_text("آب") == "اب"

    def test_key_casefold(self) -> None:
        assert unify_key("  PyThon ") == "python"

    def test_fa_digits_only(self) -> None:
        assert fa_digits("abc۱۲۳") == "abc123"


class TestPlaceholders:
    @pytest.mark.parametrize(
        "value",
        ["", "-", "N/A", "n/a", "none", "null", "نامشخص", "نامعلوم", "ذکر نشده", "؟", "[]"],
    )
    def test_placeholders(self, value: str) -> None:
        assert is_placeholder(value)

    @pytest.mark.parametrize("value", ["Python", "توافقی", "مهم نیست", "تهران"])
    def test_non_placeholders(self, value: str) -> None:
        assert not is_placeholder(value)

    @pytest.mark.parametrize(
        "value",
        ["مهم نیست", "فرقی ندارد", "not important", "no requirement", "Doesn't matter"],
    )
    def test_explicit_no(self, value: str) -> None:
        assert is_explicit_no(value)

    def test_unknown_is_placeholder_not_explicit_no(self) -> None:
        assert is_placeholder("unknown")
        assert not is_explicit_no("unknown")


class TestParens:
    def test_removed_text_returned(self) -> None:
        main, removed = strip_parentheticals("Python (بک‌اند) و SQL")
        assert main == "Python و SQL"
        assert removed == ["بک اند"]

    def test_ascii_and_fullwidth(self) -> None:
        main, removed = strip_parentheticals("React（senior）")
        assert main == "React"
        assert removed == ["senior"]

    def test_entirely_parenthetical(self) -> None:
        main, removed = strip_parentheticals("(تمام‌وقت)")
        assert main == "تمام وقت"
        assert removed == []

    def test_no_parens(self) -> None:
        main, removed = strip_parentheticals("Machine Learning")
        assert main == "Machine Learning"
        assert removed == []


class TestComposite:
    WHITELIST = {"ci/cd", "a/b testing", "ml/ai", "node.js"}

    def test_persian_and(self) -> None:
        assert split_composite("Python و SQL", self.WHITELIST) == ["Python", "SQL"]

    def test_comma(self) -> None:
        assert split_composite("React, Vue", self.WHITELIST) == ["React", "Vue"]

    def test_persian_comma(self) -> None:
        assert split_composite("React، Vue", self.WHITELIST) == ["React", "Vue"]

    def test_slash_with_spaces(self) -> None:
        assert split_composite("QLoRA / LoRA", self.WHITELIST) == ["QLoRA", "LoRA"]

    def test_slash_without_spaces(self) -> None:
        assert split_composite("QLoRA/LoRA", self.WHITELIST) == ["QLoRA", "LoRA"]

    def test_whitelist_whole_value(self) -> None:
        assert split_composite("CI/CD", self.WHITELIST) == ["CI/CD"]

    def test_whitelist_inside_composite(self) -> None:
        assert split_composite("CI/CD و Python", self.WHITELIST) == ["CI/CD", "Python"]

    def test_keep_whole_callback(self) -> None:
        assert split_composite(
            "حضوری و دورکاری", set(), keep_whole=lambda k: k == "حضوری و دورکاری"
        ) == ["حضوری و دورکاری"]

    def test_single_value(self) -> None:
        assert split_composite("Python", self.WHITELIST) == ["Python"]

    def test_dedupe(self) -> None:
        assert split_composite("Python, Python", self.WHITELIST) == ["Python"]
