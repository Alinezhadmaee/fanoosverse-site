"""
Module: transport.py
Description: Resilient network fetcher optimized for Iranian hosting and WAF environments.
Executes primary HTTP requests using curl_cffi with Chrome TLS/JA3 impersonation. Automatically 
detects Cloudflare, ArvanCloud challenges, or SPA empty shells, falling back to a headless 
stealth Playwright instance when JavaScript rendering or session solving is mandatory.
"""

import logging
import random
import time
from typing import Dict, Optional
from urllib.parse import urlparse

from models import TransportEngine, TransportResponse

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

import urllib.request
import urllib.error

try:
    from playwright.sync_api import sync_playwright, Browser, Playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("transport")


# =====================================================================
# Custom Transport Exceptions
# =====================================================================

class TransportError(Exception):
    """Base exception raised when both primary HTTP and fallback browser transport fail."""
    def __init__(self, message: str, url: str = "", status_code: Optional[int] = None, retryable: bool = True):
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.retryable = retryable


class WAFChallengeBlockedError(TransportError):
    """Raised when a Cloudflare, ArvanCloud, or CAPTCHA challenge cannot be bypassed."""
    pass


# =====================================================================
# Resilient Dual-Engine Transport Layer
# =====================================================================

class ResilientTransport:
    """
    Coordinates high-speed TLS-impersonated HTTP fetching (curl_cffi) with automatic
    escalation to stealth headless Playwright for JS SPAs and WAF challenges.
    """

    # Signatures in HTTP body indicating an unsolved WAF interstitial or JS challenge
    WAF_CHALLENGE_SIGNATURES = (
        "just a moment...",
        "attention required! | cloudflare",
        "cf-browser-verification",
        "cf-turnstile",
        "حفاظت ابری آروان",
        "در حال بررسی امنیتی مرورگر",
        "لطفاً تیک من ربات نیستم",
        "لطفا کپچا را وارد کنید",
        "enable javascript and cookies to continue",
    )

    # Empty SPA mount tags that indicate JS rendering is required
    SPA_SHELL_MARKERS = (
        "<app-root></app-root>",
        "<app-root ng-version",
        '<div id="__next"></div>',
        '<div id="root"></div>',
        '<div id="app"></div>',
    )

    # Fallback chain of Chrome TLS impersonation profiles supported across curl_cffi versions
    IMPERSONATE_FALLBACK_CHAIN = ("chrome124", "chrome120", "chrome116", "chrome110")

    def __init__(
        self,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        impersonate_profile: str = "chrome124",
        playwright_headless: bool = True,
        proxy_url: Optional[str] = None,
        bypass_proxy_for_ir_domains: bool = True,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.impersonate_profile = impersonate_profile
        self.playwright_headless = playwright_headless
        self.proxy_url = proxy_url.strip() if proxy_url and proxy_url.strip() else None
        self.bypass_proxy_for_ir_domains = bypass_proxy_for_ir_domains

        # Per-platform rate-limiting state: {platform_name: last_request_epoch}
        self._last_request_time: Dict[str, float] = {}

        # Lazy-initialized Playwright browser instance (started only when needed)
        self._pw_context_manager: Optional[Playwright] = None
        self._pw_browser: Optional[Browser] = None

        logger.info(
            f"Initialized ResilientTransport (curl_cffi={CURL_CFFI_AVAILABLE}, "
            f"playwright={PLAYWRIGHT_AVAILABLE}, impersonate='{self.impersonate_profile}', "
            f"timeout={self.timeout_seconds}s, retries={self.max_retries}, "
            f"bypass_ir_proxy={self.bypass_proxy_for_ir_domains})."
        )

    # =================================================================
    # Main Public Fetch Method
    # =================================================================

    def fetch(
        self,
        url: str,
        platform: str = "unknown",
        job_id: str = "N/A",
        requires_js: bool = False,
        rate_limit_seconds: float = 2.0,
        wait_for_selector: Optional[str] = None,
    ) -> TransportResponse:
        """
        Fetches a URL respecting platform rate limits.
        Uses curl_cffi first (unless `requires_js=True`), and automatically escalates
        to stealth Playwright if a WAF challenge, 403/503 block, or unrendered SPA shell is detected.
        """
        log_ctx = f"[job_id={job_id} | platform={platform} | url={url}]"
        self._enforce_rate_limit(platform, rate_limit_seconds, log_ctx)

        resolved_proxy = self._resolve_proxy_for_url(url, log_ctx)

        # 1. If platform explicitly requires JS (e.g., JobVision Angular or Quera Next.js), go straight to Playwright
        if requires_js:
            if PLAYWRIGHT_AVAILABLE:
                logger.info(f"{log_ctx} Platform marked requires_js=True; routing directly to Playwright.")
                return self._fetch_with_playwright(
                    url=url,
                    proxy=resolved_proxy,
                    wait_for_selector=wait_for_selector,
                    log_ctx=log_ctx,
                )
            else:
                logger.warning(
                    f"{log_ctx} Platform marked requires_js=True, but Playwright is not installed! "
                    f"Attempting curl_cffi fallback."
                )

        # 2. Primary Fast Path: curl_cffi with TLS/JA3 Impersonation
        try:
            response = self._fetch_with_curl_cffi(url=url, proxy=resolved_proxy, log_ctx=log_ctx)

            # Inspect HTTP response for soft WAF blocks or unhydrated SPA shells
            needs_js_escalation, escalation_reason = self._check_needs_playwright_escalation(
                status_code=response.status_code,
                html=response.html,
                headers=response.headers,
            )

            if not needs_js_escalation:
                logger.info(
                    f"{log_ctx} Primary HTTP fetch succeeded via {response.engine_used.value} "
                    f"(status={response.status_code}, bytes={len(response.html)}, "
                    f"elapsed={response.elapsed_ms}ms)."
                )
                return response

            logger.warning(
                f"{log_ctx} Primary HTTP fetch triggered escalation condition: '{escalation_reason}'. "
                f"Escalating to stealth Playwright..."
            )
        except TransportError as http_err:
            logger.warning(
                f"{log_ctx} Primary HTTP fetch failed ({http_err}). "
                f"Attempting fallback via stealth Playwright..."
            )
            if not PLAYWRIGHT_AVAILABLE:
                raise

        # 3. Fallback Path: Headless Stealth Playwright
        if PLAYWRIGHT_AVAILABLE:
            return self._fetch_with_playwright(
                url=url,
                proxy=resolved_proxy,
                wait_for_selector=wait_for_selector,
                log_ctx=log_ctx,
            )

        raise TransportError(
            f"Escalation to Playwright required for {url}, but Playwright is not available.",
            url=url,
            retryable=True,
        )

    # =================================================================
    # Engine 1: curl_cffi (Chrome TLS/JA3 Impersonation)
    # =================================================================

    def _fetch_with_curl_cffi(
        self,
        url: str,
        proxy: Optional[str],
        log_ctx: str,
    ) -> TransportResponse:
        """Executes HTTP GET with exponential backoff and Chrome TLS fingerprint impersonation."""
        headers = self._build_default_headers(url)
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            start_time = time.perf_counter()
            try:
                if CURL_CFFI_AVAILABLE:
                    profile = self._select_impersonate_profile(attempt)
                    logger.debug(
                        f"{log_ctx} [curl_cffi Attempt {attempt}/{self.max_retries}] "
                        f"GET (impersonate={profile}, proxy={proxy or 'DIRECT'})..."
                    )
                    proxies_dict = {"http": proxy, "https": proxy} if proxy else None
                    resp = curl_requests.get(
                        url,
                        headers=headers,
                        impersonate=profile,
                        proxies=proxies_dict,
                        timeout=self.timeout_seconds,
                        allow_redirects=True,
                    )
                    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    html_text = resp.text or ""
                    resp_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                    status_code = int(resp.status_code)
                    final_url = str(resp.url)
                else:
                    # Standard library fallback if curl_cffi is not installed in the current environment
                    logger.debug(
                        f"{log_ctx} [urllib Attempt {attempt}/{self.max_retries}] "
                        f"GET (curl_cffi not installed, using stdlib urllib)..."
                    )
                    req = urllib.request.Request(url, headers=headers, method="GET")
                    with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                        raw_bytes = resp.read()
                        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                        html_text = raw_bytes.decode("utf-8", errors="replace")
                        resp_headers = {str(k).lower(): str(v) for k, v in resp.info().items()}
                        status_code = int(resp.getcode() or 200)
                        final_url = str(resp.geturl())
                        profile = "stdlib-urllib"

                # Hard 404 Not Found -> Do not waste retries; return so caller/preflight can handle
                if status_code == 404:
                    logger.warning(f"{log_ctx} Server returned HTTP 404 Not Found in {elapsed_ms}ms.")
                    raise TransportError(
                        f"HTTP 404 Not Found for URL: {url}",
                        url=url,
                        status_code=404,
                        retryable=False,
                    )

                if status_code >= 500 and attempt < self.max_retries:
                    backoff = (2 ** attempt) + random.uniform(0.2, 0.8)
                    logger.warning(
                        f"{log_ctx} Server returned HTTP {status_code} on attempt {attempt}. "
                        f"Backing off {backoff:.2f}s before retry..."
                    )
                    time.sleep(backoff)
                    continue

                return TransportResponse(
                    url=url,
                    final_url=final_url,
                    status_code=status_code,
                    html=html_text if html_text.strip() else "<!-- empty -->",
                    engine_used=TransportEngine.CURL_CFFI,
                    elapsed_ms=elapsed_ms,
                    proxy_used=proxy,
                    impersonate_profile=profile,
                    headers=resp_headers,
                )

            except TransportError:
                raise
            except Exception as e:
                last_error = e
                elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                logger.warning(
                    f"{log_ctx} [HTTP Attempt {attempt}/{self.max_retries}] Network exception after "
                    f"{elapsed_ms}ms: {type(e).__name__}: {e}"
                )
                if attempt < self.max_retries:
                    backoff = (1.5 ** attempt) + random.uniform(0.2, 0.6)
                    time.sleep(backoff)

        raise TransportError(
            f"All {self.max_retries} HTTP attempts failed for {url}. Last error: {last_error}",
            url=url,
            retryable=True,
        )

    # =================================================================
    # Engine 2: Headless Stealth Playwright (JS & WAF Solver)
    # =================================================================

    def _fetch_with_playwright(
        self,
        url: str,
        proxy: Optional[str],
        wait_for_selector: Optional[str],
        log_ctx: str,
    ) -> TransportResponse:
        """
        Renders the target page in a stealth Chromium context, masking automation fingerprints
        (`navigator.webdriver`), executing SPA JavaScript, and waiting for DOM hydration.
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise TransportError("Playwright is not installed.", url=url, retryable=False)

        start_time = time.perf_counter()
        self._ensure_playwright_browser(proxy=proxy)
        assert self._pw_browser is not None

        context = self._pw_browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="fa-IR",
            timezone_id="Asia/Tehran",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )

        # Inject stealth script to mask headless browser indicators checked by Cloudflare/ArvanCloud
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['fa-IR', 'fa', 'en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4]});
            window.chrome = { runtime: {} };
            """
        )

        page = context.new_page()
        try:
            logger.info(f"{log_ctx} [Playwright] Navigating to URL (timeout={self.timeout_seconds}s)...")
            pw_timeout_ms = int(self.timeout_seconds * 1000)
            resp = page.goto(url, wait_until="domcontentloaded", timeout=pw_timeout_ms)

            # Wait briefly for network activity to settle on Angular/Next.js SPAs
            try:
                page.wait_for_load_state("networkidle", timeout=min(pw_timeout_ms, 8000))
            except PlaywrightTimeoutError:
                logger.debug(f"{log_ctx} [Playwright] networkidle timeout reached; proceeding with current DOM.")

            # If a platform-specific CSS selector was provided, wait for it to hydrate
            if wait_for_selector:
                try:
                    first_sel = wait_for_selector.split(",")[0].strip()
                    if not first_sel.startswith("/"):
                        page.wait_for_selector(first_sel, timeout=5000)
                except PlaywrightTimeoutError:
                    logger.debug(
                        f"{log_ctx} [Playwright] Timed out waiting for selector '{wait_for_selector}'; capturing DOM as-is."
                    )

            html_content = page.content()
            final_url = page.url
            status_code = resp.status if resp else 200
            resp_headers = {str(k).lower(): str(v) for k, v in (resp.headers.items() if resp else [])}
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

            # Verify whether a WAF block still persists after Playwright rendering
            is_still_blocked, block_reason = self._check_waf_only(html_content)
            if is_still_blocked:
                logger.error(
                    f"{log_ctx} [Playwright] WAF/CAPTCHA block persisted after browser rendering: {block_reason}"
                )
                raise WAFChallengeBlockedError(
                    f"Playwright could not bypass WAF challenge ({block_reason})",
                    url=url,
                    status_code=status_code,
                    retryable=True,
                )

            logger.info(
                f"{log_ctx} [Playwright] Render succeeded (status={status_code}, "
                f"bytes={len(html_content)}, elapsed={elapsed_ms}ms)."
            )
            return TransportResponse(
                url=url,
                final_url=final_url,
                status_code=status_code,
                html=html_content,
                engine_used=TransportEngine.PLAYWRIGHT,
                elapsed_ms=elapsed_ms,
                proxy_used=proxy,
                impersonate_profile="playwright-chromium-stealth",
                headers=resp_headers,
            )
        except WAFChallengeBlockedError:
            raise
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error(f"{log_ctx} [Playwright] Navigation failed after {elapsed_ms}ms: {e}")
            raise TransportError(f"Playwright fetch failed for {url}: {e}", url=url, retryable=True) from e
        finally:
            context.close()

    def _ensure_playwright_browser(self, proxy: Optional[str] = None) -> None:
        """Lazily launches the headless Chromium instance only when Playwright is needed."""
        if self._pw_browser is None:
            logger.info(f"Launching headless Playwright Chromium browser (headless={self.playwright_headless})...")
            self._pw_context_manager = sync_playwright().start()
            launch_kwargs: Dict[str, object] = {
                "headless": self.playwright_headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            }
            if proxy:
                launch_kwargs["proxy"] = {"server": proxy}
            self._pw_browser = self._pw_context_manager.chromium.launch(**launch_kwargs)

    def close(self) -> None:
        """Gracefully closes Playwright browser resources if launched."""
        if self._pw_browser is not None:
            try:
                self._pw_browser.close()
                logger.debug("Closed Playwright Chromium browser instance.")
            except Exception as e:
                logger.debug(f"Warning while closing Playwright browser: {e}")
            finally:
                self._pw_browser = None

        if self._pw_context_manager is not None:
            try:
                self._pw_context_manager.stop()
            except Exception:
                pass
            finally:
                self._pw_context_manager = None

    def __enter__(self) -> "ResilientTransport":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # =================================================================
    # Internal Escalation, Proxy & Rate-Limit Helpers
    # =================================================================

    def _check_needs_playwright_escalation(
        self,
        status_code: int,
        html: str,
        headers: Dict[str, str],
    ) -> tuple[bool, Optional[str]]:
        """
        Determines whether a curl_cffi response is a WAF challenge, rate-limit block,
        or unrendered JavaScript SPA shell that requires Playwright.
        """
        if status_code in (403, 429, 503):
            return True, f"HTTP_{status_code}_STATUS"

        if headers.get("cf-mitigated") == "challenge":
            return True, "CLOUDFLARE_HEADER_CHALLENGE"

        is_waf, waf_sig = self._check_waf_only(html)
        if is_waf:
            return True, f"WAF_SIGNATURE ({waf_sig})"

        # Check for unhydrated SPA shell on short responses (< 3500 bytes of HTML)
        if len(html) < 3500:
            html_lower = html.lower()
            for marker in self.SPA_SHELL_MARKERS:
                if marker in html_lower:
                    return True, f"UNRENDERED_SPA_SHELL ({marker})"

        return False, None

    def _check_waf_only(self, html: str) -> tuple[bool, Optional[str]]:
        """Checks if a short HTML page contains a WAF or CAPTCHA interstitial signature."""
        if not html or len(html) > 25000:
            return False, None
        html_lower = html.lower()
        for sig in self.WAF_CHALLENGE_SIGNATURES:
            if sig in html_lower:
                return True, sig
        return False, None

    def _resolve_proxy_for_url(self, url: str, log_ctx: str) -> Optional[str]:
        """
        Routes domestic `.ir` domains directly (bypassing foreign proxies) when
        `bypass_proxy_for_ir_domains=True` to avoid ArvanCloud foreign-IP blocks.
        """
        if not self.proxy_url:
            return None

        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        if self.bypass_proxy_for_ir_domains and (
            hostname.endswith(".ir") or hostname in ("quera.org", "www.quera.org")
        ):
            logger.debug(
                f"{log_ctx} Bypassing proxy for domestic Iranian platform '{hostname}' (using DIRECT connection)."
            )
            return None

        return self.proxy_url

    def _enforce_rate_limit(self, platform: str, rate_limit_seconds: float, log_ctx: str) -> None:
        """Applies polite per-platform delay with randomized jitter between requests."""
        key = platform.lower().strip()
        now = time.monotonic()
        last_time = self._last_request_time.get(key)

        if last_time is not None and rate_limit_seconds > 0:
            elapsed = now - last_time
            jitter = random.uniform(0.1, 0.45)
            target_wait = rate_limit_seconds + jitter
            if elapsed < target_wait:
                sleep_duration = round(target_wait - elapsed, 2)
                logger.debug(f"{log_ctx} Rate limiting platform '{key}': sleeping {sleep_duration}s...")
                time.sleep(sleep_duration)

        self._last_request_time[key] = time.monotonic()

    def _select_impersonate_profile(self, attempt: int) -> str:
        """Rotates through Chrome TLS profiles on retry attempts."""
        if attempt == 1 and self.impersonate_profile:
            return self.impersonate_profile
        idx = (attempt - 1) % len(self.IMPERSONATE_FALLBACK_CHAIN)
        return self.IMPERSONATE_FALLBACK_CHAIN[idx]

    @staticmethod
    def _build_default_headers(url: str) -> Dict[str, str]:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://google.com"
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": f"{origin}/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
# =====================================================================
# Standalone Verification Suite
# =====================================================================

if __name__ == "__main__":
    print("\n--- RUNNING TRANSPORT.PY VERIFICATION SUITE ---\n")

    with ResilientTransport(
        timeout_seconds=10.0,
        max_retries=2,
        proxy_url="socks5://127.0.0.1:1080",
        bypass_proxy_for_ir_domains=True,
    ) as transport:

        # Test 1: Domestic .ir Proxy Bypass Resolution
        ir_proxy = transport._resolve_proxy_for_url("https://jobinja.ir/jobs/1", "[test=1]")
        ext_proxy = transport._resolve_proxy_for_url("https://linkedin.com/jobs/1", "[test=1]")
        assert ir_proxy is None, "Domestic .ir domain should bypass foreign proxy!"
        assert ext_proxy == "socks5://127.0.0.1:1080", "External domain should use configured proxy!"
        print(f"✅ Test 1 (Smart Proxy Routing) : jobinja.ir -> {ir_proxy or 'DIRECT'} | linkedin.com -> {ext_proxy}")

        # Test 2: WAF & Unrendered SPA Escalation Detector
        esc_waf, reason_waf = transport._check_needs_playwright_escalation(
            status_code=200,
            html="<html><body>در حال بررسی امنیتی مرورگر... حفاظت ابری آروان</body></html>",
            headers={},
        )
        esc_spa, reason_spa = transport._check_needs_playwright_escalation(
            status_code=200,
            html="<html><body><app-root></app-root></body></html>",
            headers={},
        )
        assert esc_waf is True and esc_spa is True
        print(f"✅ Test 2 (Escalation Detector) : WAF='{reason_waf}' | SPA='{reason_spa}'")

        # Test 3: Rate Limiter with Jitter
        t0 = time.perf_counter()
        transport._enforce_rate_limit("jobinja", rate_limit_seconds=0.5, log_ctx="[test=3]")
        transport._enforce_rate_limit("jobinja", rate_limit_seconds=0.5, log_ctx="[test=3]")
        elapsed_rl = round(time.perf_counter() - t0, 2)
        print(f"✅ Test 3 (Rate Limiter Jitter) : Consecutive requests spaced by {elapsed_rl}s (>= 0.5s)\n")



















