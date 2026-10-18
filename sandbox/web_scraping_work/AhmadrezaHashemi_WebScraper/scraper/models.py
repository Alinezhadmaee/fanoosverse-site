"""
Module: models.py
Description: Defines strictly typed Pydantic data schemas representing the scraping lifecycle of a job 
advertisement. Enforces validation for SQLite queue records, HTTP transport responses, intermediate 
extraction outputs, and pre-flight quality reports. Defines the final RawJobAd handoff contract—packaging 
metadata, expiration status flags (is_expired), and sanitized raw Persian text—delivered to the Fanoos 
Supervisor and downstream entity-extraction agents.
"""

import hashlib
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

try:
    from logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("models")


# =====================================================================
# Lifecycle Enumerations
# =====================================================================

class JobLedgerStatus(str, Enum):
    """Transactional states for a job URL inside scraper_ledger.db."""
    PENDING = "PENDING"                         # Discovered by url_seeder, waiting to be leased
    LEASED = "LEASED"                           # Currently locked by a worker/scraper instance
    COMPLETED = "COMPLETED"                     # Successfully scraped, sanitized, and ACKed
    FAILED_RETRYABLE = "FAILED_RETRYABLE"       # Network/WAF error; eligible for retry until max_retries
    REJECTED_PREFLIGHT = "REJECTED_PREFLIGHT"   # Corrupted/empty page rejected by preflight_gate
    DEAD_LETTER = "DEAD_LETTER"                 # Exhausted all retries; permanently parked


class ExtractionTier(str, Enum):
    """Identifies which tier in cascade_extractor.py produced the final payload."""
    TIER_1_JSON_LD = "TIER_1_JSON_LD"
    TIER_2_SELECTORS = "TIER_2_SELECTORS"
    TIER_3_HEURISTIC = "TIER_3_HEURISTIC"
    NONE = "NONE"


class TransportEngine(str, Enum):
    """Identifies which HTTP/Browser engine in transport.py fetched the HTML."""
    CURL_CFFI = "CURL_CFFI"
    PLAYWRIGHT = "PLAYWRIGHT"
    UNKNOWN = "UNKNOWN"


# =====================================================================
# URL Normalization & Deterministic Hashing Helpers
# =====================================================================

TRACKING_QUERY_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source"
}


def normalize_job_url(raw_url: str) -> str:
    """
    Canonicalizes a job URL by stripping tracking query parameters and URL fragments
    so identical job postings always produce the exact same SHA-256 hash.
    """
    if not raw_url or not raw_url.strip():
        raise ValueError("Job URL cannot be empty.")

    cleaned = raw_url.strip()
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid job URL (missing scheme or domain): '{raw_url}'")

    # Filter out marketing/tracking query parameters while preserving functional ones
    filtered_query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_QUERY_PARAMS
    ]
    filtered_query.sort(key=lambda item: item[0])

    normalized_path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    canonical = urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        normalized_path,
        parsed.params,
        urlencode(filtered_query, doseq=True),
        ""  # Strip #fragment
    ))
    return canonical


def compute_url_sha256(url: str) -> str:
    """Returns the 64-character lowercase SHA-256 hex digest of the normalized URL."""
    canonical = normalize_job_url(url)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generate_deterministic_job_id(platform: str, url: str) -> str:
    """Generates a traceable, deterministic job_id (e.g., 'jobinja-8f3a1b9c2d4e')."""
    url_hash = compute_url_sha256(url)
    clean_platform = (platform or "unknown").lower().strip()
    return f"{clean_platform}-{url_hash[:12]}"


# =====================================================================
# 1. State Ledger Schema (state_ledger.py)
# =====================================================================

class LedgerJobRecord(BaseModel):
    """Represents a single job URL record persisted inside scraper_ledger.db."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    job_id: str = Field(..., description="Deterministic identifier: <platform>-<12_char_hash>")
    url: str = Field(..., description="Canonical job advertisement URL")
    url_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hash of canonical URL")
    platform: str = Field(..., min_length=2, description="Source platform key from sites_config.yaml")
    status: JobLedgerStatus = Field(default=JobLedgerStatus.PENDING)
    retry_count: int = Field(default=0, ge=0)
    leased_until: Optional[datetime] = Field(default=None)
    last_error: Optional[str] = Field(default=None)
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def populate_hashes_and_ids(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_url = data.get("url")
            platform = data.get("platform", "unknown")
            if raw_url:
                canonical_url = normalize_job_url(raw_url)
                data["url"] = canonical_url
                if not data.get("url_hash"):
                    data["url_hash"] = compute_url_sha256(canonical_url)
                if not data.get("job_id"):
                    data["job_id"] = generate_deterministic_job_id(platform, canonical_url)
        return data


# =====================================================================
# 2. HTTP / Browser Transport Schema (transport.py)
# =====================================================================

class TransportResponse(BaseModel):
    """Encapsulates the network response returned by curl_cffi or Playwright."""
    model_config = ConfigDict(extra="ignore")

    url: str = Field(..., description="Requested URL")
    final_url: str = Field(..., description="Final resolved URL after any HTTP redirects")
    status_code: int = Field(..., ge=100, le=599)
    html: str = Field(..., min_length=1, description="Decoded HTML document string")
    engine_used: TransportEngine = Field(default=TransportEngine.CURL_CFFI)
    elapsed_ms: float = Field(..., ge=0.0)
    proxy_used: Optional[str] = Field(default=None)
    impersonate_profile: Optional[str] = Field(default=None)
    headers: Dict[str, str] = Field(default_factory=dict)


# =====================================================================
# 3. Intermediate Extraction Schema (Bridges cascade_extractor.py)
# =====================================================================

class ExtractedPrimitives(BaseModel):
    """
    Validated Pydantic representation of the raw primitives isolated by cascade_extractor.py.
    """
    model_config = ConfigDict(extra="ignore")

    raw_title: Optional[str] = Field(default=None)
    raw_company: Optional[str] = Field(default=None)
    raw_location: Optional[str] = Field(default=None)
    raw_text: str = Field(..., min_length=1, description="Raw extracted Persian job description and info-box text")
    extraction_tier_used: ExtractionTier = Field(default=ExtractionTier.NONE)
    tiers_attempted: List[str] = Field(default_factory=list)
    extraction_duration_ms: float = Field(default=0.0, ge=0.0)
    warnings: List[str] = Field(default_factory=list)

    @classmethod
    def from_dataclass(cls, dc_obj: Any) -> "ExtractedPrimitives":
        """Converts CascadeExtractor's ExtractionResult dataclass into a validated Pydantic model."""
        raw_dict = {
            "raw_title": getattr(dc_obj, "raw_title", None),
            "raw_company": getattr(dc_obj, "raw_company", None),
            "raw_location": getattr(dc_obj, "raw_location", None),
            "raw_text": getattr(dc_obj, "raw_text", None) or "",
            "extraction_tier_used": getattr(dc_obj, "extraction_tier_used", "NONE"),
            "tiers_attempted": list(getattr(dc_obj, "tiers_attempted", [])),
            "extraction_duration_ms": float(getattr(dc_obj, "extraction_duration_ms", 0.0)),
            "warnings": list(getattr(dc_obj, "warnings", [])),
        }
        return cls.model_validate(raw_dict)


# =====================================================================
# 4. Pre-Flight Quality & Expiration Report Schema (preflight_gate.py)
# =====================================================================

class PreflightReport(BaseModel):
    """
    Data quality and structural verification report produced by preflight_gate.py.
    Note: Expired ads (is_expired=True) PASS the gate so historical AI job data is preserved.
    """
    model_config = ConfigDict(extra="ignore")

    passed: bool = Field(..., description="True if content is valid and ready for downstream AI agents")
    is_expired: bool = Field(
        default=False,
        description="True if listing contains expired/closed indicators ('آگهی منقضی شده'); kept for market analysis"
    )
    expired_matched_phrase: Optional[str] = Field(
        default=None,
        description="The exact Persian/English phrase that triggered is_expired=True"
    )
    char_count: int = Field(..., ge=0)
    word_count: int = Field(..., ge=0)
    persian_char_ratio: float = Field(
        ..., ge=0.0, le=1.0,
        description="Ratio of Persian/Arabic script characters to total alphabetic characters"
    )
    rejection_reason: Optional[str] = Field(
        default=None,
        description="Populated only when passed=False (e.g., 'BELOW_MIN_CHAR_THRESHOLD', 'ERROR_404_PAGE')"
    )
    warnings: List[str] = Field(default_factory=list)


# =====================================================================
# 5. Final Handoff Contract: RawJobAd (To Supervisor & Mani's Agent)
# =====================================================================

class RawJobAd(BaseModel):
    """
    The final standardized output contract of Ahmadreza Hashemi's Web Scraper pipeline.
    Delivers clean, RTL-sanitized raw Persian job text and provenance metadata to the 
    Fanoos Supervisor and Mani Yahyazadeh's entity-extraction agent.
    """
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    job_id: str = Field(..., description="Unique deterministic ID (<platform>-<12_char_hash>)")
    url: str = Field(..., description="Canonical source URL of the job advertisement")
    url_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hex digest of the canonical URL")
    platform: str = Field(..., min_length=2, description="Job board identifier (e.g., jobinja, jobvision)")
    scraped_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the ad was scraped"
    )

    # Historical Market Analysis Metadata (Never filtered out)
    is_expired: bool = Field(
        default=False,
        description="Indicates whether the job listing was already closed/expired at scrape time"
    )

    # Core Sanitized Persian Primitives for Downstream Entity Extraction
    raw_title: str = Field(..., min_length=2, description="RTL-sanitized job title")
    raw_company: Optional[str] = Field(default=None, description="RTL-sanitized company name if available")
    raw_location: Optional[str] = Field(default=None, description="RTL-sanitized location string if available")
    raw_text: str = Field(
        ...,
        min_length=20,
        description="Complete RTL-sanitized raw Persian job text (including merged info-box tags and full description)"
    )

    # Provenance & Telemetry Metadata
    extraction_tier: ExtractionTier = Field(default=ExtractionTier.TIER_2_SELECTORS)
    transport_engine: TransportEngine = Field(default=TransportEngine.CURL_CFFI)
    request_duration_ms: float = Field(default=0.0, ge=0.0)
    preflight_report: PreflightReport

    @field_validator("raw_title", "raw_text")
    @classmethod
    def ensure_non_blank_text(cls, v: str, info: Any) -> str:
        cleaned = v.strip()
        if not cleaned:
            logger.error(f"Validation failed: '{info.field_name}' cannot be blank or whitespace-only.")
            raise ValueError(f"'{info.field_name}' cannot be empty or whitespace.")
        return cleaned

    @model_validator(mode="before")
    @classmethod
    def auto_populate_identifiers(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_url = data.get("url")
            platform = data.get("platform", "unknown")
            if raw_url:
                canonical = normalize_job_url(raw_url)
                data["url"] = canonical
                if not data.get("url_hash"):
                    data["url_hash"] = compute_url_sha256(canonical)
                if not data.get("job_id"):
                    data["job_id"] = generate_deterministic_job_id(platform, canonical)
        return data

    def to_jsonl_dict(self) -> Dict[str, Any]:
        """Serializes the RawJobAd into a JSON-compatible dictionary with ISO-8601 timestamps."""
        return self.model_dump(mode="json")

    def to_jsonl_line(self) -> str:
        """Serializes the RawJobAd into a single UTF-8 JSONL line (preserving Persian characters)."""
        return self.model_dump_json()


# =====================================================================
# Standalone Verification Suite
# =====================================================================

if __name__ == "__main__":
    print("\n--- RUNNING MODELS.PY SCHEMA & CONTRACT VERIFICATION ---\n")

    # 1. Test URL Normalization & Deterministic Hashing (stripping UTM parameters)
    dirty_url = "https://jobinja.ir/companies/fanoos/jobs/ai-eng?utm_source=telegram&utm_medium=social#apply"
    ledger_item = LedgerJobRecord(url=dirty_url, platform="jobinja")
    print(f"✅ Canonical URL : {ledger_item.url}")
    print(f"✅ Deterministic ID: {ledger_item.job_id}")
    print(f"✅ SHA-256 Hash  : {ledger_item.url_hash}")

    # 2. Test Expired Job Ad Preservation in RawJobAd Contract
    sample_report = PreflightReport(
        passed=True,
        is_expired=True,
        expired_matched_phrase="مهلت ارسال رزومه برای این آگهی تمام شده است",
        char_count=245,
        word_count=42,
        persian_char_ratio=0.82,
    )

    raw_ad = RawJobAd(
        url=dirty_url,
        platform="jobinja",
        is_expired=sample_report.is_expired,
        raw_title="استخدام مهندس هوش مصنوعی (AI Engineer)",
        raw_company="شرکت فناوری فانوس",
        raw_location="تهران",
        raw_text=(
            "[مشخصات و تگ‌های آگهی]\nنوع همکاری: تمام وقت | مهارت‌ها: Python, LangGraph, Qdrant\n\n"
            "[شرح کامل موقعیت شغلی]\nمهلت ارسال رزومه برای این آگهی تمام شده است. "
            "ما به دنبال یک مهندس هوش مصنوعی مسلط به مدل‌های زبانی بزرگ و معماری RAG هستیم."
        ),
        extraction_tier=ExtractionTier.TIER_2_SELECTORS,
        transport_engine=TransportEngine.CURL_CFFI,
        request_duration_ms=310.5,
        preflight_report=sample_report,
    )

    print(f"✅ RawJobAd Validated (is_expired={raw_ad.is_expired}, passed={raw_ad.preflight_report.passed})")
    print(f"✅ JSONL Output Sample:\n{raw_ad.to_jsonl_line()}\n")



