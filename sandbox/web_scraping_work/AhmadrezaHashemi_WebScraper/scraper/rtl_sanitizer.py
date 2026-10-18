"""
Module: rtl_sanitizer.py
Description: Deterministic Persian and RTL text normalization engine. Strips bidirectional control 
markers (LRM/RLM) while strictly preserving Zero-Width Non-Joiners (ZWNJ / \u200c). Normalizes Arabic 
orthography (Yeh/Kaf) to standard Persian, converts Persian/Arabic numerals to ASCII, and repairs 
token boundary gluing between Persian and Latin technical terms without introducing semantic distortion.
"""

import html
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

try:
    from logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("rtl_sanitizer")


# =====================================================================
# Telemetry & Batch Result Dataclasses
# =====================================================================

@dataclass
class SanitizationMetrics:
    """Tracks exact character and token repairs performed during sanitization."""
    original_length: int = 0
    sanitized_length: int = 0
    bidi_controls_stripped: int = 0
    arabic_chars_normalized: int = 0
    numerals_converted: int = 0
    token_boundaries_repaired: int = 0
    zwnj_preserved_count: int = 0
    duration_ms: float = 0.0


@dataclass
class SanitizedPrimitives:
    """Holds sanitized job fields alongside aggregate sanitization telemetry."""
    raw_title: str
    raw_company: Optional[str]
    raw_location: Optional[str]
    raw_text: str
    metrics: SanitizationMetrics


# =====================================================================
# Core Deterministic RTL & Persian Sanitizer
# =====================================================================

class RTLSanitizer:
    """
    Deterministic Unicode, Orthography, Numeral, and Token-Boundary normalizer
    designed for Iranian technical job advertisements.
    """

    ZWNJ = "\u200c"

    # 1. Bidirectional & invisible formatting controls to strip (EXCLUDING \u200c ZWNJ!)
    BIDI_AND_CONTROL_REGEX = re.compile(
        r"["
        r"\u200b"          # Zero-Width Space (ZWSP)
        r"\u200d"          # Zero-Width Joiner (ZWJ)
        r"\u200e\u200f"    # LRM, RLM
        r"\u202a-\u202e"   # LRE, RLE, PDF, LRO, RLO
        r"\u2060"          # Word Joiner
        r"\u2066-\u2069"   # LRI, RLI, FSI, PDI
        r"\ufeff"          # UTF-8 BOM / Zero-Width No-Break Space
        r"\u00ad"          # Soft Hyphen
        r"\u0640"          # Arabic Tatweel / Kashida (ـ)
        r"]"
    )

    # 2. Non-standard horizontal whitespace characters to convert to standard space ' '
    HORIZONTAL_SPACE_REGEX = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000\t]+")

    # 3. Arabic-to-Persian character translation table (Kaf, Yeh variants)
    ARABIC_TO_PERSIAN_MAP = str.maketrans({
        "\u0643": "\u06a9",  # Arabic Kaf (ك) -> Persian Kaf (ک)
        "\u064a": "\u06cc",  # Arabic Yeh (ي) -> Persian Yeh (ی)
        "\u0649": "\u06cc",  # Alef Maksura (ى) -> Persian Yeh (ی)
        "\u06d2": "\u06cc",  # Urdu Yeh Barree (ے) -> Persian Yeh (ی)
        "\u06c0": "\u0647\u0654",  # Heh with Yeh above (ۀ) -> standard Heh + Hamza
    })
    ARABIC_CHARS_DETECT_REGEX = re.compile(r"[\u0643\u064a\u0649\u06d2\u06c0]")

    # 4. Persian & Arabic-Indic numeral translation table -> ASCII 0-9
    NUMERAL_TRANSLATION_MAP = str.maketrans({
        # Persian digits (۰-۹)
        "\u06f0": "0", "\u06f1": "1", "\u06f2": "2", "\u06f3": "3", "\u06f4": "4",
        "\u06f5": "5", "\u06f6": "6", "\u06f7": "7", "\u06f8": "8", "\u06f9": "9",
        # Arabic-Indic digits (٠-٩)
        "\u0660": "0", "\u0661": "1", "\u0662": "2", "\u0663": "3", "\u0664": "4",
        "\u0665": "5", "\u0666": "6", "\u0667": "7", "\u0668": "8", "\u0669": "9",
    })
    NON_ASCII_DIGIT_REGEX = re.compile(r"[\u06f0-\u06f9\u0660-\u0669]")

    # 5. ZWNJ Cleanup Regexes
    MULTIPLE_ZWNJ_REGEX = re.compile(r"\u200c{2,}")
    ORPHAN_ZWNJ_AROUND_SPACE_REGEX = re.compile(r"(?:\s+\u200c+|\u200c+\s+)")

    # 6. Token Boundary De-Gluing Regexes
    # Persian letters range: \u0621-\u0655, \u067e (پ), \u0686 (چ), \u0698 (ژ), \u06a9 (ک), \u06af (گ), \u06cc (ی)
    PERSIAN_TO_LATIN_OR_DIGIT_REGEX = re.compile(
        r"([\u0621-\u0655\u067e\u0686\u0698\u06a9\u06af\u06cc])([A-Za-z0-9])"
    )
    LATIN_OR_DIGIT_TO_PERSIAN_REGEX = re.compile(
        r"([A-Za-z0-9])([\u0621-\u0655\u067e\u0686\u0698\u06a9\u06af\u06cc])"
    )

    # 7. Whitespace & Linebreak Cleanup
    MULTI_SPACE_REGEX = re.compile(r"[ ]{2,}")
    EXCESSIVE_NEWLINES_REGEX = re.compile(r"\n{3,}")

    def __init__(self, repair_token_boundaries: bool = True, convert_numerals_to_ascii: bool = True):
        self.repair_token_boundaries = repair_token_boundaries
        self.convert_numerals_to_ascii = convert_numerals_to_ascii
        logger.debug(
            f"Initialized RTLSanitizer (repair_token_boundaries={self.repair_token_boundaries}, "
            f"convert_numerals_to_ascii={self.convert_numerals_to_ascii})"
        )

    def sanitize_text(
        self,
        text: Optional[str],
        field_name: str = "text",
        job_id: str = "N/A",
        platform: str = "unknown",
    ) -> str:
        """
        Sanitizes a single string field and logs the transformation telemetry.
        """
        sanitized, _ = self.sanitize_with_metrics(
            text=text, field_name=field_name, job_id=job_id, platform=platform
        )
        return sanitized

    def sanitize_with_metrics(
        self,
        text: Optional[str],
        field_name: str = "text",
        job_id: str = "N/A",
        platform: str = "unknown",
    ) -> tuple[str, SanitizationMetrics]:
        """
        Executes the 7-stage deterministic sanitization pipeline on a string and returns
        both the cleaned string and detailed SanitizationMetrics.
        """
        start_time = time.perf_counter()
        metrics = SanitizationMetrics()

        if not text:
            return "", metrics

        metrics.original_length = len(text)
        log_ctx = f"[job_id={job_id} | platform={platform} | field={field_name}]"

        # -----------------------------------------------------------------
        # Stage 1: HTML Entity Unescaping & Named ZWNJ Recovery
        # -----------------------------------------------------------------
        # Explicitly convert &zwnj; before general unescaping to guarantee ZWNJ retention
        working = re.sub(r"&zwnj;?", self.ZWNJ, text, flags=re.IGNORECASE)
        working = html.unescape(working)

        # Normalize Unicode via NFC while keeping ZWNJ intact
        working = unicodedata.normalize("NFC", working)

        # -----------------------------------------------------------------
        # Stage 2: Strip Bidirectional Controls (LRM/RLM/BOM/Tatweel)
        # -----------------------------------------------------------------
        bidi_matches = self.BIDI_AND_CONTROL_REGEX.findall(working)
        metrics.bidi_controls_stripped = len(bidi_matches)
        if metrics.bidi_controls_stripped > 0:
            working = self.BIDI_AND_CONTROL_REGEX.sub("", working)

        # -----------------------------------------------------------------
        # Stage 3: Normalize Arabic Yeh/Kaf Orthography to Standard Persian
        # -----------------------------------------------------------------
        arabic_matches = self.ARABIC_CHARS_DETECT_REGEX.findall(working)
        metrics.arabic_chars_normalized = len(arabic_matches)
        if metrics.arabic_chars_normalized > 0:
            working = working.translate(self.ARABIC_TO_PERSIAN_MAP)

        # -----------------------------------------------------------------
        # Stage 4: Convert Persian & Arabic-Indic Numerals to ASCII (0-9)
        # -----------------------------------------------------------------
        if self.convert_numerals_to_ascii:
            digit_matches = self.NON_ASCII_DIGIT_REGEX.findall(working)
            metrics.numerals_converted = len(digit_matches)
            if metrics.numerals_converted > 0:
                working = working.translate(self.NUMERAL_TRANSLATION_MAP)

        # -----------------------------------------------------------------
        # Stage 5: ZWNJ (\u200c) Deduplication & Orphan Cleanup
        # -----------------------------------------------------------------
        # Collapse 2+ consecutive ZWNJs into a single ZWNJ
        working = self.MULTIPLE_ZWNJ_REGEX.sub(self.ZWNJ, working)
        # If a ZWNJ is immediately adjacent to whitespace, replace with a normal space
        working = self.ORPHAN_ZWNJ_AROUND_SPACE_REGEX.sub(" ", working)
        # Strip leading/trailing ZWNJs at start or end of text
        working = working.strip(self.ZWNJ)

        # -----------------------------------------------------------------
        # Stage 6: Repair Persian <-> Latin/Digit Token Boundary Gluing
        # -----------------------------------------------------------------
        if self.repair_token_boundaries:
            working, count_p2l = self.PERSIAN_TO_LATIN_OR_DIGIT_REGEX.subn(r"\1 \2", working)
            working, count_l2p = self.LATIN_OR_DIGIT_TO_PERSIAN_REGEX.subn(r"\1 \2", working)
            metrics.token_boundaries_repaired = count_p2l + count_l2p

        # -----------------------------------------------------------------
        # Stage 7: Linebreak & Horizontal Whitespace Standardization
        # -----------------------------------------------------------------
        working = working.replace("\r\n", "\n").replace("\r", "\n")
        working = self.HORIZONTAL_SPACE_REGEX.sub(" ", working)

        # Clean horizontal spaces around newlines while preserving paragraph structure
        lines = [self.MULTI_SPACE_REGEX.sub(" ", line).strip() for line in working.split("\n")]
        working = "\n".join(lines)
        working = self.EXCESSIVE_NEWLINES_REGEX.sub("\n\n", working).strip()

        metrics.sanitized_length = len(working)
        metrics.zwnj_preserved_count = working.count(self.ZWNJ)
        metrics.duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        logger.debug(
            f"{log_ctx} Sanitization complete in {metrics.duration_ms}ms -> "
            f"len: {metrics.original_length}->{metrics.sanitized_length}, "
            f"arabic_fixed={metrics.arabic_chars_normalized}, "
            f"digits_fixed={metrics.numerals_converted}, "
            f"bidi_stripped={metrics.bidi_controls_stripped}, "
            f"boundaries_repaired={metrics.token_boundaries_repaired}, "
            f"zwnj_preserved={metrics.zwnj_preserved_count}."
        )

        return working, metrics

    def sanitize_job_primitives(
        self,
        raw_title: Optional[str],
        raw_company: Optional[str],
        raw_location: Optional[str],
        raw_text: Optional[str],
        job_id: str = "N/A",
        platform: str = "unknown",
    ) -> SanitizedPrimitives:
        """
        Batch-sanitizes all 4 core job primitives extracted by cascade_extractor.py
        and logs an aggregate summary for the job listing.
        """
        log_ctx = f"[job_id={job_id} | platform={platform}]"
        logger.info(f"{log_ctx} Running RTL & Persian sanitization across extracted job primitives...")

        clean_title, m_title = self.sanitize_with_metrics(raw_title, "raw_title", job_id, platform)
        clean_company, m_comp = self.sanitize_with_metrics(raw_company, "raw_company", job_id, platform)
        clean_location, m_loc = self.sanitize_with_metrics(raw_location, "raw_location", job_id, platform)
        clean_text, m_text = self.sanitize_with_metrics(raw_text, "raw_text", job_id, platform)

        total_metrics = SanitizationMetrics(
            original_length=m_title.original_length + m_comp.original_length + m_loc.original_length + m_text.original_length,
            sanitized_length=m_title.sanitized_length + m_comp.sanitized_length + m_loc.sanitized_length + m_text.sanitized_length,
            bidi_controls_stripped=(
                m_title.bidi_controls_stripped + m_comp.bidi_controls_stripped +
                m_loc.bidi_controls_stripped + m_text.bidi_controls_stripped
            ),
            arabic_chars_normalized=(
                m_title.arabic_chars_normalized + m_comp.arabic_chars_normalized +
                m_loc.arabic_chars_normalized + m_text.arabic_chars_normalized
            ),
            numerals_converted=(
                m_title.numerals_converted + m_comp.numerals_converted +
                m_loc.numerals_converted + m_text.numerals_converted
            ),
            token_boundaries_repaired=(
                m_title.token_boundaries_repaired + m_comp.token_boundaries_repaired +
                m_loc.token_boundaries_repaired + m_text.token_boundaries_repaired
            ),
            zwnj_preserved_count=(
                m_title.zwnj_preserved_count + m_comp.zwnj_preserved_count +
                m_loc.zwnj_preserved_count + m_text.zwnj_preserved_count
            ),
            duration_ms=round(
                m_title.duration_ms + m_comp.duration_ms + m_loc.duration_ms + m_text.duration_ms, 2
            ),
        )

        logger.info(
            f"{log_ctx} RTL Sanitization finished in {total_metrics.duration_ms}ms "
            f"(arabic_fixed={total_metrics.arabic_chars_normalized}, "
            f"digits_fixed={total_metrics.numerals_converted}, "
            f"glued_tokens_fixed={total_metrics.token_boundaries_repaired}, "
            f"zwnj_preserved={total_metrics.zwnj_preserved_count})."
        )

        return SanitizedPrimitives(
            raw_title=clean_title,
            raw_company=clean_company if clean_company else None,
            raw_location=clean_location if clean_location else None,
            raw_text=clean_text,
            metrics=total_metrics,
        )


# =====================================================================
# Standalone Verification Suite
# =====================================================================

if __name__ == "__main__":
    print("\n--- RUNNING RTL_SANITIZER.PY VERIFICATION SUITE ---\n")
    sanitizer = RTLSanitizer()

    # Dirty Persian job text containing:
    # 1. Arabic Yeh (ي) and Kaf (ك) -> 'برنامه نويسي', 'پيشرفته', 'كدنويسي'
    # 2. Bidirectional LRM/RLM (\u200e, \u200f) and Tatweel (ـ) -> 'مهنـــدس'
    # 3. Valid ZWNJ (\u200c) that MUST be preserved -> 'برنامه‌نویس', 'پایگاه‌داده', 'می‌باشیم'
    # 4. Glued Persian-Latin/Number tokens -> 'بهPythonوFastAPIبا حداقل۳سال', 'برDockerوQdrantدر'
    # 5. Persian & Arabic-Indic digits -> '۳', '٤٥,٠٠٠,٠٠٠'
    dirty_title = "\u200fاستخدام مهنـــدس ارشد هوش مصنوعي (Senior AI Engineer)\u200e"
    dirty_company = "شركت دادهكاوان فانوس"
    dirty_location = "تهران، ونك"
    dirty_text = (
        "ما در تيم فني به دنبال يك برنامه‌نویس مسلط بهPythonوFastAPIبا حداقل۳سال سابقه كار هستيم.\n\n\n\n"
        "شرايط احراز: تسلط كامل برDockerوQdrantدر محيطLinux (نسخه 24.04) و تجربه كدنويسي تميز.\n"
        "حقوق پيشنهادي: از ٤٥,٠٠٠,٠٠٠ تومان؛ مشتاق دريافت رزومه شما می‌باشیم."
    )

    result = sanitizer.sanitize_job_primitives(
        raw_title=dirty_title,
        raw_company=dirty_company,
        raw_location=dirty_location,
        raw_text=dirty_text,
        job_id="TEST-RTL-1",
        platform="jobinja",
    )

    print(f"✅ Clean Title    : {result.raw_title}")
    print(f"✅ Clean Company  : {result.raw_company}")
    print(f"✅ Clean Location : {result.raw_location}")
    print(f"✅ Clean Text     :\n{result.raw_text}\n")
    print(
        f"✅ Telemetry      : Arabic Fixed={result.metrics.arabic_chars_normalized} | "
        f"Digits Fixed={result.metrics.numerals_converted} | "
        f"Bidi Stripped={result.metrics.bidi_controls_stripped} | "
        f"Glued Tokens Repaired={result.metrics.token_boundaries_repaired} | "
        f"ZWNJs Preserved={result.metrics.zwnj_preserved_count}\n"
    )