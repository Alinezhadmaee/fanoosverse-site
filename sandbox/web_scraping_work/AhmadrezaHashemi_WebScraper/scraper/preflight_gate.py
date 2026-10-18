"""
Module: preflight_gate.py
Description: Validates scraped and sanitized content against data quality and structural criteria.
Detects and flags expired or closed job listings ('آگهی منقضی شده') as metadata without rejecting them, 
ensuring complete historical coverage of Iran's AI job market. Evaluates minimum character thresholds, 
Persian script ratios, and document structure to reject only corrupted, empty, or error-page payloads 
before downstream AI agents consume API quota.
"""
import logging
import re
from typing import List, Optional, Tuple

from models import PreflightReport

try:
    from logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("preflight_gate")


# =====================================================================
# Custom Exception (Optional Strict Mode Hook)
# =====================================================================

class PreflightRejectedError(Exception):
    """Raised when a caller requests strict exception raising on pre-flight rejection."""
    def __init__(self, reason: str, report: PreflightReport):
        super().__init__(f"Pre-flight gate rejected job ad: {reason}")
        self.reason = reason
        self.report = report


# =====================================================================
# Core Pre-Flight Content & Quality Gate
# =====================================================================

class PreflightGate:
    """
    Evaluates extracted and sanitized job advertisement primitives prior to downstream
    agent handoff. Preserves expired listings with metadata flags while blocking
    corrupted, empty, or soft-404 pages.
    """

    # Signatures indicating a job listing is closed/expired (Flagged as metadata, NOT rejected)
    EXPIRED_SIGNATURES: List[str] = [
        "آگهی منقضی شده",
        "این آگهی منقضی شده",
        "مهلت ارسال رزومه برای این آگهی تمام شده",
        "مهلت ارسال رزومه تمام شده",
        "مهلت آگهی به پایان رسیده",
        "فرصت شغلی منقضی شده",
        "این موقعیت شغلی بسته شده",
        "آگهی استخدام منقضی",
        "غیرفعال شده است",
        "استخدام در این ردیف شغلی به پایان رسیده",
        "job posting has expired",
        "no longer accepting applications",
        "this job is closed",
    ]

    # Signatures indicating a Soft-404, deleted post, or server crash page
    SOFT_404_SIGNATURES: List[str] = [
        "صفحه مورد نظر یافت نشد",
        "صفحه‌ای که به دنبال آن بودید پیدا نشد",
        "خطای ۴۰۴",
        "خطای 404",
        "آگهی مورد نظر یافت نشد",
        "این صفحه وجود ندارد",
        "404 not found",
        "page not found",
        "502 bad gateway",
        "503 service temporarily unavailable",
        "internal server error",
    ]

    # Signatures indicating a login wall hiding the actual job description on short pages
    LOGIN_WALL_SIGNATURES: List[str] = [
        "برای مشاهده متن کامل آگهی وارد شوید",
        "جهت مشاهده اطلاعات تماس و متن آگهی وارد حساب کاربری",
        "لطفا برای مشاهده جزئیات آگهی وارد شوید",
    ]

    # Regex matching Persian/Arabic Unicode script characters
    PERSIAN_CHAR_REGEX = re.compile(r"[\u0600-\u06FF\uFB50-\uFDFF\uFE70-\uFEFF]")
    # Regex matching Latin alphabetic characters
    LATIN_CHAR_REGEX = re.compile(r"[A-Za-z]")
    # Common UTF-8 / Windows-1256 Mojibake sequences when Persian text is mis-decoded as Latin-1
    MOJIBAKE_REGEX = re.compile(r"(?:Ø[\x80-\xBF]|Ù[\x80-\xBF]|Ú[\x80-\xBF]|Û[\x80-\xBF]){3,}")

    def __init__(
        self,
        min_description_chars: int = 80,
        min_title_chars: int = 2,
        min_persian_ratio_warn: float = 0.15,
    ):
        self.min_description_chars = min_description_chars
        self.min_title_chars = min_title_chars
        self.min_persian_ratio_warn = min_persian_ratio_warn
        logger.debug(
            f"Initialized PreflightGate (min_description_chars={self.min_description_chars}, "
            f"min_title_chars={self.min_title_chars}, "
            f"min_persian_ratio_warn={self.min_persian_ratio_warn})"
        )

    def evaluate(
        self,
        raw_title: Optional[str],
        raw_text: Optional[str],
        raw_company: Optional[str] = None,
        job_id: str = "N/A",
        platform: str = "unknown",
        url: str = "",
    ) -> PreflightReport:
        """
        Runs the complete pre-flight inspection suite on a scraped job ad and returns
        a validated PreflightReport. Never discards expired ads unless the body is empty.
        """
        log_ctx = f"[job_id={job_id} | platform={platform}]"
        title_clean = (raw_title or "").strip()
        text_clean = (raw_text or "").strip()
        warnings: List[str] = []

        char_count = len(text_clean)
        word_count = len(text_clean.split()) if text_clean else 0
        persian_ratio = self._compute_persian_ratio(f"{title_clean} {text_clean}")

        logger.info(
            f"{log_ctx} Starting Pre-Flight Gate inspection "
            f"(title_len={len(title_clean)}, char_count={char_count}, "
            f"word_count={word_count}, persian_ratio={persian_ratio:.2f})."
        )

        # -----------------------------------------------------------------
        # STEP 1: Check for Expired / Closed Job Indicators (Metadata Flag Only)
        # -----------------------------------------------------------------
        is_expired, matched_expired_phrase = self._detect_expiration(title_clean, text_clean)
        if is_expired:
            logger.info(
                f"{log_ctx} [Pre-Flight Step 1] Expired job listing detected via phrase: "
                f"'{matched_expired_phrase}'. Flagging is_expired=True and PRESERVING for market analysis."
            )
            warnings.append(f"EXPIRED_LISTING_PRESERVED: '{matched_expired_phrase}'")
        else:
            logger.debug(f"{log_ctx} [Pre-Flight Step 1] Listing appears active (no expiration phrases matched).")

        # -----------------------------------------------------------------
        # STEP 2: Check for Corrupted Encoding / Mojibake
        # -----------------------------------------------------------------
        if self._has_mojibake(title_clean) or self._has_mojibake(text_clean):
            reason = "CORRUPTED_ENCODING_MOJIBAKE"
            logger.error(
                f"{log_ctx} [Pre-Flight Step 2] REJECTED ({reason}): Detected mis-decoded Latin-1/UTF-8 mojibake."
            )
            return self._build_report(
                passed=False,
                is_expired=is_expired,
                expired_phrase=matched_expired_phrase,
                char_count=char_count,
                word_count=word_count,
                persian_ratio=persian_ratio,
                rejection_reason=reason,
                warnings=warnings,
            )

        # -----------------------------------------------------------------
        # STEP 3: Check for Soft-404 or Server Error Pages
        # -----------------------------------------------------------------
        soft_404_match = self._detect_soft_404(title_clean, text_clean, char_count)
        if soft_404_match:
            reason = f"SOFT_404_OR_ERROR_PAGE ({soft_404_match})"
            logger.warning(
                f"{log_ctx} [Pre-Flight Step 3] REJECTED ({reason}): Page is a dead link or error stub."
            )
            return self._build_report(
                passed=False,
                is_expired=is_expired,
                expired_phrase=matched_expired_phrase,
                char_count=char_count,
                word_count=word_count,
                persian_ratio=persian_ratio,
                rejection_reason=reason,
                warnings=warnings,
            )

        # -----------------------------------------------------------------
        # STEP 4: Validate Job Title Presence
        # -----------------------------------------------------------------
        if len(title_clean) < self.min_title_chars:
            reason = f"MISSING_OR_SHORT_TITLE (len={len(title_clean)} < {self.min_title_chars})"
            logger.warning(f"{log_ctx} [Pre-Flight Step 4] REJECTED ({reason}).")
            return self._build_report(
                passed=False,
                is_expired=is_expired,
                expired_phrase=matched_expired_phrase,
                char_count=char_count,
                word_count=word_count,
                persian_ratio=persian_ratio,
                rejection_reason=reason,
                warnings=warnings,
            )

        # -----------------------------------------------------------------
        # STEP 5: Validate Minimum Description Character Threshold & Login Walls
        # -----------------------------------------------------------------
        # Measure effective description length after stripping the expiration banner itself
        effective_char_count = char_count
        if is_expired and matched_expired_phrase:
            effective_char_count = len(text_clean.replace(matched_expired_phrase, "").strip())

        if effective_char_count < self.min_description_chars:
            # Check if a login wall caused the truncation
            login_wall = self._detect_login_wall(text_clean)
            if login_wall:
                reason = f"TRUNCATED_BY_LOGIN_WALL ('{login_wall}')"
            elif is_expired:
                reason = (
                    f"EXPIRED_STUB_NO_CONTENT (effective_chars={effective_char_count} < "
                    f"{self.min_description_chars}; platform removed job body)"
                )
            else:
                reason = (
                    f"BELOW_MIN_CHAR_THRESHOLD (chars={char_count} < {self.min_description_chars})"
                )

            logger.warning(f"{log_ctx} [Pre-Flight Step 5] REJECTED ({reason}).")
            return self._build_report(
                passed=False,
                is_expired=is_expired,
                expired_phrase=matched_expired_phrase,
                char_count=char_count,
                word_count=word_count,
                persian_ratio=persian_ratio,
                rejection_reason=reason,
                warnings=warnings,
            )

        # -----------------------------------------------------------------
        # STEP 6: Evaluate Persian vs. English Script Ratio
        # -----------------------------------------------------------------
        if persian_ratio < self.min_persian_ratio_warn:
            # Verify whether the text is a legitimate English-language tech job ad vs. pure symbols
            latin_count = len(self.LATIN_CHAR_REGEX.findall(text_clean))
            if latin_count >= (self.min_description_chars // 2):
                warn_msg = (
                    f"LOW_PERSIAN_RATIO_ENGLISH_AD (persian_ratio={persian_ratio:.2f}; "
                    f"contains {latin_count} Latin chars)"
                )
                warnings.append(warn_msg)
                logger.info(
                    f"{log_ctx} [Pre-Flight Step 6] {warn_msg} -> Allowing English-language tech job ad."
                )
            else:
                reason = f"NON_LINGUISTIC_GARBAGE (persian_ratio={persian_ratio:.2f}, latin_chars={latin_count})"
                logger.warning(f"{log_ctx} [Pre-Flight Step 6] REJECTED ({reason}).")
                return self._build_report(
                    passed=False,
                    is_expired=is_expired,
                    expired_phrase=matched_expired_phrase,
                    char_count=char_count,
                    word_count=word_count,
                    persian_ratio=persian_ratio,
                    rejection_reason=reason,
                    warnings=warnings,
                )

        if not raw_company or not raw_company.strip():
            warnings.append("MISSING_COMPANY_NAME")
            logger.debug(f"{log_ctx} [Pre-Flight] Note: raw_company is empty; downstream agent will infer from text.")

        logger.info(
            f"{log_ctx} Pre-Flight Gate PASSED (is_expired={is_expired}, "
            f"chars={char_count}, words={word_count}, warnings={len(warnings)})."
        )
        return self._build_report(
            passed=True,
            is_expired=is_expired,
            expired_phrase=matched_expired_phrase,
            char_count=char_count,
            word_count=word_count,
            persian_ratio=persian_ratio,
            rejection_reason=None,
            warnings=warnings,
        )

    # =================================================================
    # Internal Detection Helpers
    # =================================================================

    def _detect_expiration(self, title: str, text: str) -> Tuple[bool, Optional[str]]:
        """Checks if the title or text contains any known expired/closed job phrases."""
        combined_lower = f"{title}\n{text}".lower()
        for phrase in self.EXPIRED_SIGNATURES:
            if phrase.lower() in combined_lower:
                return True, phrase
        return False, None

    def _detect_soft_404(self, title: str, text: str, char_count: int) -> Optional[str]:
        """
        Detects Soft-404 or server error messages.
        Checks title unconditionally, and checks body text when page content is short (< 600 chars).
        """
        title_lower = title.lower()
        for sig in self.SOFT_404_SIGNATURES:
            if sig.lower() in title_lower:
                return sig

        if char_count < 600:
            text_lower = text.lower()
            for sig in self.SOFT_404_SIGNATURES:
                if sig.lower() in text_lower:
                    return sig
        return None

    def _detect_login_wall(self, text: str) -> Optional[str]:
        text_lower = text.lower()
        for sig in self.LOGIN_WALL_SIGNATURES:
            if sig.lower() in text_lower:
                return sig
        return None

    def _has_mojibake(self, text: str) -> bool:
        """Detects garbled UTF-8 bytes decoded as Latin-1/CP1252."""
        if not text:
            return False
        return bool(self.MOJIBAKE_REGEX.search(text))

    def _compute_persian_ratio(self, text: str) -> float:
        """Calculates the ratio of Persian/Arabic script characters to total alphabetic letters."""
        if not text:
            return 0.0
        persian_chars = len(self.PERSIAN_CHAR_REGEX.findall(text))
        latin_chars = len(self.LATIN_CHAR_REGEX.findall(text))
        total_alpha = persian_chars + latin_chars
        if total_alpha == 0:
            return 0.0
        return round(persian_chars / total_alpha, 4)

    @staticmethod
    def _build_report(
        passed: bool,
        is_expired: bool,
        expired_phrase: Optional[str],
        char_count: int,
        word_count: int,
        persian_ratio: float,
        rejection_reason: Optional[str],
        warnings: List[str],
    ) -> PreflightReport:
        return PreflightReport(
            passed=passed,
            is_expired=is_expired,
            expired_matched_phrase=expired_phrase,
            char_count=char_count,
            word_count=word_count,
            persian_char_ratio=persian_ratio,
            rejection_reason=rejection_reason,
            warnings=warnings,
        )


# =====================================================================
# Standalone Verification Suite
# =====================================================================

if __name__ == "__main__":
    print("\n--- RUNNING PREFLIGHT_GATE.PY VERIFICATION SUITE ---\n")
    gate = PreflightGate(min_description_chars=60)

    # Test 1: Active Persian AI Job Ad -> Should PASS (is_expired=False)
    r1 = gate.evaluate(
        raw_title="استخدام مهندس یادگیری عمیق (Deep Learning Engineer)",
        raw_company="فانوس هوش مصنوعی",
        raw_text="ما در تیم فانوس به دنبال یک متخصص هوش مصنوعی مسلط به پایتون، PyTorch و پردازش زبان طبیعی فارسی هستیم.",
        job_id="TEST-ACTIVE-1",
        platform="jobinja"
    )
    print(f"✅ Test 1 (Active Ad)   : passed={r1.passed} | is_expired={r1.is_expired} | ratio={r1.persian_char_ratio}")

    # Test 2: Expired Persian AI Job Ad with full description -> Should PASS & PRESERVE (is_expired=True)
    r2 = gate.evaluate(
        raw_title="استخدام متخصص کلان‌داده و مهندس داده",
        raw_company="شرکت داده‌کاوان",
        raw_text=(
            "مهلت ارسال رزومه برای این آگهی تمام شده است.\n"
            "شرح شغل: طراحی و پیاده‌سازی پایپ‌لاین‌های داده، تسلط به SQL، پایتون، Apache Spark و پایگاه‌داده Qdrant."
        ),
        job_id="TEST-EXPIRED-2",
        platform="jobvision"
    )
    print(
        f"✅ Test 2 (Expired Ad)  : passed={r2.passed} | is_expired={r2.is_expired} | "
        f"phrase='{r2.expired_matched_phrase}'"
    )

    # Test 3: Soft-404 Dead Page -> Should REJECT (passed=False)
    r3 = gate.evaluate(
        raw_title="صفحه مورد نظر یافت نشد - خطای ۴۰۴",
        raw_company=None,
        raw_text="متاسفانه آگهی مورد نظر یافت نشد یا حذف گردیده است.",
        job_id="TEST-404-3",
        platform="iranestekhdam"
    )
    print(f"✅ Test 3 (Soft 404)    : passed={r3.passed} | rejection_reason='{r3.rejection_reason}'")

    # Test 4: English-language Tech Job Ad on Quera -> Should PASS with Warning
    r4 = gate.evaluate(
        raw_title="Senior AI & LLM Infrastructure Engineer",
        raw_company="Fanoos Labs",
        raw_text=(
            "We are looking for a Senior AI Engineer with strong experience in Python, FastAPI, "
            "vector databases (Qdrant), and RAG pipelines to join our Tehran team."
        ),
        job_id="TEST-ENG-4",
        platform="quera"
    )
    print(f"✅ Test 4 (English Ad)  : passed={r4.passed} | warnings={r4.warnings}\n")