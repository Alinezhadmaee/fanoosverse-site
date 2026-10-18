"""
Module: scraper_service.py
Description: Central orchestration facade for the Fanoos web scraping pipeline. Exposes a unified 
interface for downstream agent workflows to lease a single job listing, coordinate transport, extraction, 
sanitization, and pre-flight validation. Dispatches transactional success (ACK) and failure (NACK) events 
back to the state ledger based on downstream execution results.
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from cascade_extractor import (
    BotChallengeDetectedError,
    CascadeExtractor,
    ExtractionError,
    ExtractionResult,
    UnrenderedSPAError,
)
from config import AppConfig, PlatformConfig, get_config
from logger import get_logger, log_context
from models import (
    ExtractionTier,
    JobLedgerStatus,
    LedgerJobRecord,
    RawJobAd,
    TransportEngine,
    TransportResponse,
)
from preflight_gate import PreflightGate
from rtl_sanitizer import RTLSanitizer
from state_ledger import StateLedger
from transport import ResilientTransport, TransportError, WAFChallengeBlockedError
from url_seeder import URLSeeder

logger = get_logger(__name__)

SCRAPER_ROOT_DIR = Path(__file__).resolve().parent


# =====================================================================
# Central Scraper Orchestration Service
# =====================================================================

class ScraperService:
    """
    Unified facade coordinating URL seeding, atomic SQLite leasing, resilient HTTP/Playwright
    fetching, 3-tier HTML extraction, RTL sanitization, pre-flight quality gating, and
    transactional ACK/NACK state updates.
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        ledger: Optional[StateLedger] = None,
        transport: Optional[ResilientTransport] = None,
        extractor: Optional[CascadeExtractor] = None,
        sanitizer: Optional[RTLSanitizer] = None,
        preflight_gate: Optional[PreflightGate] = None,
        seeder: Optional[URLSeeder] = None,
    ):
        self.config = config or get_config()

        self.ledger = ledger or StateLedger(
            db_path=self.config.env.ledger_db_path,
            lease_timeout_seconds=self.config.env.lease_timeout_seconds,
            max_retries=self.config.env.max_retries,
        )

        self._owns_transport = transport is None
        self.transport = transport or ResilientTransport(
            timeout_seconds=self.config.env.http_timeout_seconds,
            max_retries=self.config.env.max_retries,
            impersonate_profile=self.config.env.chrome_impersonate_version,
            playwright_headless=self.config.env.playwright_headless,
            proxy_url=self.config.env.proxy_url,
            bypass_proxy_for_ir_domains=self.config.env.bypass_proxy_for_ir_domains,
        )

        self.extractor = extractor or CascadeExtractor(
            min_description_chars=self.config.env.min_description_chars
        )
        self.sanitizer = sanitizer or RTLSanitizer(
            repair_token_boundaries=True,
            convert_numerals_to_ascii=True,
        )
        self.preflight_gate = preflight_gate or PreflightGate(
            min_description_chars=self.config.env.min_description_chars
        )
        self.seeder = seeder or URLSeeder(ledger=self.ledger, transport=self.transport)

        # In-memory session telemetry counters
        self.session_metrics: Dict[str, int] = {
            "leased": 0,
            "completed_ack": 0,
            "expired_preserved": 0,
            "rejected_preflight": 0,
            "failed_nack": 0,
            "js_escalations": 0,
        }

        logger.info("Initialized ScraperService orchestration facade.")

    def close(self) -> None:
        """Releases underlying browser/network resources cleanly."""
        if self._owns_transport and self.transport is not None:
            self.transport.close()
            logger.debug("Closed ScraperService transport resources.")

    def __enter__(self) -> "ScraperService":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # =================================================================
    # 1. Discovery / Seeding Delegation
    # =================================================================

    def seed_urls(
        self,
        platform: Optional[str] = None,
        max_pages_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Triggers URL discovery for a single platform or all enabled platforms,
        registering new job links into StateLedger as PENDING.
        """
        if platform:
            logger.info(f"Triggering URL discovery seed for single platform: '{platform}'.")
            return {platform: self.seeder.seed_platform(platform, max_pages_override=max_pages_override)}

        logger.info("Triggering URL discovery seed across all enabled platforms.")
        return self.seeder.seed_all_platforms(max_pages_override=max_pages_override)

    # =================================================================
    # 2. Single-Item Lease & Full Pipeline Execution
    # =================================================================

    def lease_and_scrape_next(
        self,
        platform: Optional[str] = None,
        auto_ack: bool = True,
    ) -> Optional[RawJobAd]:
        """
        Atomically leases the next available job URL from StateLedger and runs it
        through Transport -> CascadeExtractor -> RTLSanitizer -> PreflightGate.

        Args:
            platform: Optional platform filter (e.g., 'jobinja').
            auto_ack: If True, immediately marks the job COMPLETED in StateLedger upon
                      passing PreflightGate. If False, leaves the job in LEASED state so
                      the downstream Supervisor Agent can explicitly call `ack_job()` or `nack_job()`.

        Returns:
            Validated `RawJobAd` handoff contract if successful, or `None` if no eligible
            job is available or if the leased job failed/rejected.
        """
        record = self.ledger.lease_next_job(platform=platform)
        if record is None:
            logger.debug(f"No pending jobs available in ledger (platform_filter={platform or 'ALL'}).")
            return None

        self.session_metrics["leased"] += 1
        pipeline_start = time.perf_counter()

        with log_context(job_id=record.job_id, platform=record.platform, url=record.url):
            logger.info(
                f"Starting scraping pipeline for leased job "
                f"(retry_count={record.retry_count}, auto_ack={auto_ack})."
            )

            p_config = self._get_platform_config_safe(record.platform)
            selectors_dict = self._build_selectors_dict(p_config)
            requires_js = p_config.requires_js if p_config else False
            rate_limit = p_config.rate_limit_seconds if p_config else 2.5
            wait_selector = self._get_primary_wait_selector(selectors_dict)

            try:
                # ---------------------------------------------------------
                # STEP A & B: Transport Fetch + 3-Tier Cascade Extraction
                # (Includes automatic Playwright retry if DOM trap/SPA detected)
                # ---------------------------------------------------------
                transport_resp, extraction_res = self._fetch_and_extract_with_escalation(
                    record=record,
                    requires_js=requires_js,
                    rate_limit=rate_limit,
                    wait_selector=wait_selector,
                    selectors_dict=selectors_dict,
                )

                # ---------------------------------------------------------
                # STEP C: Deterministic RTL & Persian Sanitization
                # ---------------------------------------------------------
                sanitized = self.sanitizer.sanitize_job_primitives(
                    raw_title=extraction_res.raw_title,
                    raw_company=extraction_res.raw_company,
                    raw_location=extraction_res.raw_location,
                    raw_text=extraction_res.raw_text,
                    job_id=record.job_id,
                    platform=record.platform,
                )

                # Fallback title recovery if title was empty but body text exists
                final_title = sanitized.raw_title
                if not final_title and sanitized.raw_text:
                    first_line = sanitized.raw_text.splitlines()[0].strip()
                    final_title = first_line[:120] if len(first_line) >= 2 else "Job Advertisement"
                    logger.debug(f"Recovered missing raw_title from first line of text: '{final_title}'")

                # ---------------------------------------------------------
                # STEP D: Pre-Flight Quality & Expiration Gate
                # (Preserves expired ads with is_expired=True metadata!)
                # ---------------------------------------------------------
                preflight_report = self.preflight_gate.evaluate(
                    raw_title=final_title,
                    raw_text=sanitized.raw_text,
                    raw_company=sanitized.raw_company,
                    job_id=record.job_id,
                    platform=record.platform,
                    url=record.url,
                )

                if not preflight_report.passed:
                    reason = preflight_report.rejection_reason or "PREFLIGHT_VALIDATION_FAILED"
                    self.ledger.reject_preflight_job(record.job_id, reason)
                    self.session_metrics["rejected_preflight"] += 1
                    logger.warning(
                        f"Job rejected by PreflightGate (reason='{reason}'). "
                        f"Marked as REJECTED_PREFLIGHT in ledger."
                    )
                    return None

                if preflight_report.is_expired:
                    self.session_metrics["expired_preserved"] += 1

                # ---------------------------------------------------------
                # STEP E: Construct Final Validated RawJobAd Contract
                # ---------------------------------------------------------
                total_duration_ms = round((time.perf_counter() - pipeline_start) * 1000, 2)
                tier_enum = self._parse_extraction_tier(extraction_res.extraction_tier_used)

                raw_job_ad = RawJobAd(
                    job_id=record.job_id,
                    url=record.url,
                    url_hash=record.url_hash,
                    platform=record.platform,
                    is_expired=preflight_report.is_expired,
                    raw_title=final_title,
                    raw_company=sanitized.raw_company,
                    raw_location=sanitized.raw_location,
                    raw_text=sanitized.raw_text,
                    extraction_tier=tier_enum,
                    transport_engine=transport_resp.engine_used,
                    request_duration_ms=transport_resp.elapsed_ms,
                    preflight_report=preflight_report,
                )

                # ---------------------------------------------------------
                # STEP F: Transactional ACK (Immediate or Deferred to Supervisor)
                # ---------------------------------------------------------
                if auto_ack:
                    self.ledger.ack_job(record.job_id)
                    self.session_metrics["completed_ack"] += 1
                    logger.info(
                        f"Pipeline SUCCEEDED and ACKed in {total_duration_ms}ms "
                        f"(tier={raw_job_ad.extraction_tier.value}, "
                        f"engine={raw_job_ad.transport_engine.value}, "
                        f"is_expired={raw_job_ad.is_expired}, "
                        f"chars={preflight_report.char_count})."
                    )
                else:
                    logger.info(
                        f"Pipeline SUCCEEDED in {total_duration_ms}ms (auto_ack=False). "
                        f"Awaiting downstream Supervisor ACK/NACK for job_id={record.job_id}."
                    )

                return raw_job_ad

            except TransportError as t_err:
                self.session_metrics["failed_nack"] += 1
                self.ledger.nack_job(
                    job_id=record.job_id,
                    error_message=f"TransportError: {t_err}",
                    retryable=t_err.retryable,
                )
                return None

            except (BotChallengeDetectedError, UnrenderedSPAError, ExtractionError) as e_err:
                self.session_metrics["failed_nack"] += 1
                self.ledger.nack_job(
                    job_id=record.job_id,
                    error_message=f"{type(e_err).__name__}: {e_err}",
                    retryable=True,
                )
                return None

            except Exception as unexpected_err:
                self.session_metrics["failed_nack"] += 1
                logger.error(
                    f"Unhandled exception while processing job_id={record.job_id}: {unexpected_err}",
                    exc_info=True,
                )
                self.ledger.nack_job(
                    job_id=record.job_id,
                    error_message=f"Unexpected {type(unexpected_err).__name__}: {unexpected_err}",
                    retryable=True,
                )
                return None

    # =================================================================
    # 3. Batch Processing Helper (Used by main.py & Batch Workflows)
    # =================================================================

    def scrape_batch(
        self,
        limit: int = 10,
        platform: Optional[str] = None,
        auto_ack: bool = True,
        max_attempts_multiplier: int = 3,
    ) -> List[RawJobAd]:
        """
        Iteratively leases and scrapes jobs from the ledger until `limit` valid `RawJobAd`
        contracts are collected or the ledger queue is exhausted.
        """
        if limit <= 0:
            return []

        collected: List[RawJobAd] = []
        max_attempts = max(limit * max_attempts_multiplier, limit + 5)
        attempts = 0

        logger.info(
            f"Starting batch scrape (target_valid_ads={limit}, "
            f"platform_filter={platform or 'ALL'}, max_attempts={max_attempts})."
        )

        while len(collected) < limit and attempts < max_attempts:
            # Check if any eligible PENDING or FAILED_RETRYABLE records remain before leasing
            stats = self.ledger.get_ledger_stats()["by_status"]
            eligible_remaining = stats.get("PENDING", 0) + stats.get("FAILED_RETRYABLE", 0)
            if eligible_remaining == 0:
                # Reclaim any expired leases just in case
                reclaimed = self.ledger.reclaim_expired_leases()
                if reclaimed == 0:
                    logger.info("Ledger queue exhausted (0 PENDING or FAILED_RETRYABLE jobs remaining).")
                    break

            attempts += 1
            ad = self.lease_and_scrape_next(platform=platform, auto_ack=auto_ack)
            if ad is not None:
                collected.append(ad)

        logger.info(
            f"Batch scrape finished -> collected={len(collected)}/{limit} valid RawJobAd(s) "
            f"across {attempts} lease attempt(s)."
        )
        return collected

    # =================================================================
    # 4. Downstream Supervisor Agent Callbacks (ACK / NACK / Telemetry)
    # =================================================================

    def ack_job(self, job_id: str) -> bool:
        """Explicitly marks a leased job as COMPLETED (used when auto_ack=False)."""
        result = self.ledger.ack_job(job_id)
        if result:
            self.session_metrics["completed_ack"] += 1
        return result

    def nack_job(self, job_id: str, error_message: str, retryable: bool = True) -> JobLedgerStatus:
        """Explicitly marks a leased job as failed (used when downstream agent rejects/fails)."""
        self.session_metrics["failed_nack"] += 1
        return self.ledger.nack_job(job_id=job_id, error_message=error_message, retryable=retryable)

    def get_pipeline_status(self) -> Dict[str, Any]:
        """Returns combined SQLite ledger statistics and current session execution metrics."""
        return {
            "session_metrics": dict(self.session_metrics),
            "ledger_stats": self.ledger.get_ledger_stats(),
        }

    # =================================================================
    # Internal Escalation & Configuration Helpers
    # =================================================================

    def _fetch_and_extract_with_escalation(
        self,
        record: LedgerJobRecord,
        requires_js: bool,
        rate_limit: float,
        wait_selector: Optional[str],
        selectors_dict: Optional[Dict[str, Union[str, List[str]]]],
    ) -> Tuple[TransportResponse, ExtractionResult]:
        """
        Executes `transport.fetch()` followed by `extractor.extract()`.
        If `extractor.extract()` detects a DOM-level CAPTCHA/WAF challenge or unrendered SPA shell
        on a page that was fetched via `curl_cffi`, automatically escalates to Playwright once.
        """
        transport_resp = self.transport.fetch(
            url=record.url,
            platform=record.platform,
            job_id=record.job_id,
            requires_js=requires_js,
            rate_limit_seconds=rate_limit,
            wait_for_selector=wait_selector if requires_js else None,
        )

        try:
            extraction_res = self.extractor.extract(
                html=transport_resp.html,
                url=record.url,
                platform=record.platform,
                selectors=selectors_dict,
                job_id=record.job_id,
            )
            return transport_resp, extraction_res

        except (UnrenderedSPAError, BotChallengeDetectedError) as dom_trap:
            # If we haven't tried Playwright yet, escalate to headless browser rendering!
            if transport_resp.engine_used != TransportEngine.PLAYWRIGHT:
                self.session_metrics["js_escalations"] += 1
                logger.warning(
                    f"Extractor caught {type(dom_trap).__name__} after {transport_resp.engine_used.value} fetch. "
                    f"Escalating job_id={record.job_id} to Playwright JS rendering..."
                )
                pw_resp = self.transport.fetch(
                    url=record.url,
                    platform=record.platform,
                    job_id=record.job_id,
                    requires_js=True,
                    rate_limit_seconds=0.5,
                    wait_for_selector=wait_selector,
                )
                pw_extraction = self.extractor.extract(
                    html=pw_resp.html,
                    url=record.url,
                    platform=record.platform,
                    selectors=selectors_dict,
                    job_id=record.job_id,
                )
                return pw_resp, pw_extraction
            raise

    def _get_platform_config_safe(self, platform: str) -> Optional[PlatformConfig]:
        try:
            return self.config.get_platform(platform)
        except KeyError:
            logger.warning(
                f"Platform '{platform}' not found in sites_config.yaml; proceeding with Tier 1/Tier 3 defaults."
            )
            return None

    @staticmethod
    def _build_selectors_dict(
        p_config: Optional[PlatformConfig],
    ) -> Optional[Dict[str, Union[str, List[str]]]]:
        if not p_config or not p_config.selectors:
            return None
        sel = p_config.selectors
        return {
            "title": sel.title,
            "company": sel.company,
            "location": sel.location,
            "info_box": sel.info_box,
            "description": sel.description,
        }

    @staticmethod
    def _get_primary_wait_selector(
        selectors_dict: Optional[Dict[str, Union[str, List[str]]]],
    ) -> Optional[str]:
        if not selectors_dict:
            return None
        desc_sel = selectors_dict.get("description")
        if not desc_sel:
            return None
        candidates = [desc_sel] if isinstance(desc_sel, str) else list(desc_sel)
        for c in candidates:
            c_clean = c.strip()
            if c_clean and not c_clean.startswith("/") and not c_clean.startswith("("):
                return c_clean
        return None

    @staticmethod
    def _parse_extraction_tier(tier_str: str) -> ExtractionTier:
        try:
            return ExtractionTier(tier_str)
        except ValueError:
            return ExtractionTier.NONE


# =====================================================================
# Standalone Verification Suite
# =====================================================================

if __name__ == "__main__":
    print("\n--- RUNNING SCRAPER_SERVICE.PY END-TO-END ORCHESTRATION SUITE ---\n")

    test_db_path = SCRAPER_ROOT_DIR / "logs" / "test_service_ledger.db"
    if test_db_path.exists():
        test_db_path.unlink()

    # Create a deterministic MockTransport so we test the full pipeline offline
    class MockOfflineTransport(ResilientTransport):
        def fetch(
            self,
            url: str,
            platform: str = "unknown",
            job_id: str = "N/A",
            requires_js: bool = False,
            rate_limit_seconds: float = 0.0,
            wait_for_selector: Optional[str] = None,
        ) -> TransportResponse:
            if "active-ai-job" in url:
                html = """
                <html><body>
                  <div class="c-jobView__titleText"><h1>استخدام مهندس هوش مصنوعی (Senior AI Engineer)</h1></div>
                  <h2 class="c-companyHeader__title">شرکت فناوری فانوس</h2>
                  <ul class="c-infoBox">
                    <li class="c-infoBox__item"><h4>محل کار</h4><span>تهران</span></li>
                    <li class="c-infoBox__item"><h4>مهارت‌ها</h4><span>Python, FastAPI, Qdrant, LangGraph</span></li>
                  </ul>
                  <div class="s-jobDesc">
                    ما در تیم فنی فانوس به دنبال یک مهندس هوش مصنوعی مسلط بهPythonوFastAPIبا حداقل۳سال سابقه کار هستیم.
                  </div>
                </body></html>
                """
            elif "expired-ai-job" in url:
                html = """
                <html><body>
                  <div class="c-jobView__titleText"><h1>استخدام متخصص یادگیری ماشین و پردازش زبان طبیعی</h1></div>
                  <h2 class="c-companyHeader__title">آزمایشگاه داده‌کاوی پارس</h2>
                  <div class="s-jobDesc">
                    مهلت ارسال رزومه برای این آگهی تمام شده است.
                    شرح موقعیت شغلی: توسعه مدل‌های زبانی بزرگ، تسلط کامل بر PyTorch و پایگاه‌داده برداری Qdrant.
                  </div>
                </body></html>
                """
            else:
                html = """
                <html><body>
                  <h1>صفحه مورد نظر یافت نشد - خطای ۴۰۴</h1>
                  <p>متاسفانه آگهی مورد نظر یافت نشد.</p>
                </body></html>
                """
            return TransportResponse(
                url=url,
                final_url=url,
                status_code=200,
                html=html,
                engine_used=TransportEngine.CURL_CFFI,
                elapsed_ms=42.5,
            )

    ledger = StateLedger(db_path=test_db_path)
    mock_transport = MockOfflineTransport()

    with ScraperService(ledger=ledger, transport=mock_transport) as service:
        # Seed 3 test URLs: 1 active, 1 expired (must be preserved!), and 1 soft-404 (must be rejected!)
        ledger.register_urls(
            platform="jobinja",
            urls=[
                "https://jobinja.ir/companies/fanoos/jobs/active-ai-job",
                "https://jobinja.ir/companies/fanoos/jobs/expired-ai-job",
                "https://jobinja.ir/companies/fanoos/jobs/missing-404-job",
            ],
        )

        # 1. Test Batch Scrape (auto_ack=True)
        ads = service.scrape_batch(limit=5, platform="jobinja", auto_ack=True)
        assert len(ads) == 2, f"Expected 2 valid ads (active + expired), got {len(ads)}"
        assert ads[0].is_expired is False
        assert ads[1].is_expired is True
        print(f"✅ Test 1 (Active Job Ad)  : id={ads[0].job_id} | is_expired={ads[0].is_expired} | title='{ads[0].raw_title}'")
        print(f"✅ Test 2 (Expired Job Ad) : id={ads[1].job_id} | is_expired={ads[1].is_expired} (PRESERVED!)")

        # 2. Test Deferred Supervisor ACK Mode (auto_ack=False)
        ledger.register_urls(
            platform="jobinja",
            urls=["https://jobinja.ir/companies/fanoos/jobs/active-ai-job-supervisor"],
        )
        leased_ad = service.lease_and_scrape_next(platform="jobinja", auto_ack=False)
        assert leased_ad is not None
        assert ledger.get_job(leased_ad.job_id).status == JobLedgerStatus.LEASED
        service.ack_job(leased_ad.job_id)
        assert ledger.get_job(leased_ad.job_id).status == JobLedgerStatus.COMPLETED
        print(f"✅ Test 3 (Supervisor ACK) : Deferred lease -> explicit ACK succeeded for {leased_ad.job_id}")

        # 3. Verify Pipeline Telemetry
        status = service.get_pipeline_status()
        print(f"✅ Test 4 (Session Metrics): {status['session_metrics']}")
        print(f"✅ Test 5 (Ledger Status)  : {status['ledger_stats']['by_status']}\n")

    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{test_db_path}{suffix}")
        if p.exists():
            p.unlink()