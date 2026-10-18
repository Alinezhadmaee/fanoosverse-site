"""
Module: url_seeder.py
Description: Executes the URL discovery phase across configured Iranian job boards. 
Navigates platform listing and pagination pages defined in sites_config.yaml, extracts 
individual job advertisement URLs, normalizes relative paths and tracking parameters, 
and bulk-registers newly discovered links into state_ledger.py as PENDING records.
"""

import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Union
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from config import PlatformConfig, get_config
from logger import log_context
from state_ledger import StateLedger
from transport import ResilientTransport, TransportError

try:
    from lxml import etree
    LXML_AVAILABLE = True
except ImportError:
    LXML_AVAILABLE = False

try:
    from logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("url_seeder")


# =====================================================================
# Module Constants
# =====================================================================

SCRAPER_ROOT_DIR = Path(__file__).resolve().parent

# Non-job paths commonly matched by broad <a> selectors that must be ignored
IGNORED_PATH_SEGMENTS = (
    "/login",
    "/register",
    "/signup",
    "/signin",
    "/about",
    "/contact",
    "/privacy",
    "/terms",
    "/faq",
    "/blog",
    "/employer",
)


# =====================================================================
# Core URL Discovery & Seeder Engine
# =====================================================================

class URLSeeder:
    """
    Crawls platform discovery and pagination listing pages, extracts job advertisement URLs
    using CSS or XPath selectors from sites_config.yaml, preserves chronological listing order,
    and bulk-registers discovered URLs into StateLedger.
    """

    def __init__(
        self,
        ledger: Optional[StateLedger] = None,
        transport: Optional[ResilientTransport] = None,
    ):
        self.config = get_config()
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
        logger.debug(
            f"Initialized URLSeeder (lxml={LXML_AVAILABLE}, owns_transport={self._owns_transport})."
        )

    def close(self) -> None:
        """Closes internal transport resources if instantiated by this URLSeeder."""
        if self._owns_transport and self.transport is not None:
            self.transport.close()
            logger.debug("Closed URLSeeder internal ResilientTransport instance.")

    def __enter__(self) -> "URLSeeder":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # =================================================================
    # Public Seeding Entrypoints
    # =================================================================

    def seed_platform(
        self,
        platform_name: str,
        max_pages_override: Optional[int] = None,
    ) -> Dict[str, int]:
        """
        Runs the discovery crawl for a single platform across all configured discovery_urls
        and pagination limits, then bulk-registers discovered job URLs into StateLedger.
        """
        start_time = time.perf_counter()
        key = platform_name.lower().strip()
        p_config = self.config.get_platform(key)

        if not p_config.enabled:
            logger.warning(f"[platform={key}] Platform is disabled in sites_config.yaml. Skipping seed.")
            return {
                "pages_crawled": 0,
                "total_input": 0,
                "inserted_new": 0,
                "duplicates_skipped": 0,
                "invalid_urls": 0,
            }

        max_pages = max_pages_override if max_pages_override is not None else p_config.max_pages_per_seed
        logger.info(
            f"[platform={key}] Starting URL discovery across {len(p_config.discovery_urls)} "
            f"seed URL(s) (max_pages_per_seed={max_pages}, requires_js={p_config.requires_js})."
        )

        ordered_discovered_urls: List[str] = []
        total_pages_crawled = 0

        for idx, seed_url_obj in enumerate(p_config.discovery_urls, start=1):
            seed_url = str(seed_url_obj)
            urls_from_seed, pages_crawled = self._crawl_seed_url(
                platform=key,
                seed_url=seed_url,
                p_config=p_config,
                max_pages=max_pages,
                seed_index=idx,
            )
            ordered_discovered_urls.extend(urls_from_seed)
            total_pages_crawled += pages_crawled

        # Deduplicate in-memory while preserving exact chronological discovery order
        unique_ordered_urls = list(dict.fromkeys(ordered_discovered_urls))

        registration_stats = self.ledger.register_urls(platform=key, urls=unique_ordered_urls)
        registration_stats["pages_crawled"] = total_pages_crawled
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        logger.info(
            f"[platform={key}] Discovery finished in {elapsed_ms}ms -> "
            f"pages_crawled={total_pages_crawled}, unique_links_found={len(unique_ordered_urls)}, "
            f"inserted_new={registration_stats['inserted_new']}, "
            f"duplicates_skipped={registration_stats['duplicates_skipped']}."
        )
        return registration_stats

    def seed_all_platforms(
        self,
        max_pages_override: Optional[int] = None,
    ) -> Dict[str, Dict[str, int]]:
        """Runs URL discovery across all enabled platforms in sites_config.yaml."""
        enabled_platforms = self.config.get_enabled_platforms()
        logger.info(
            f"Starting multi-platform URL seeding for {len(enabled_platforms)} enabled platform(s): "
            f"{list(enabled_platforms.keys())}"
        )
        results: Dict[str, Dict[str, int]] = {}

        for plat_name in enabled_platforms:
            try:
                results[plat_name] = self.seed_platform(
                    platform_name=plat_name,
                    max_pages_override=max_pages_override,
                )
            except Exception as e:
                logger.error(
                    f"[platform={plat_name}] Unhandled error during platform seeding: {e}",
                    exc_info=True,
                )
                results[plat_name] = {
                    "pages_crawled": 0,
                    "total_input": 0,
                    "inserted_new": 0,
                    "duplicates_skipped": 0,
                    "invalid_urls": 0,
                }

        return results

    # =================================================================
    # Internal Crawl & Link Extraction Logic
    # =================================================================

    def _crawl_seed_url(
        self,
        platform: str,
        seed_url: str,
        p_config: PlatformConfig,
        max_pages: int,
        seed_index: int,
    ) -> tuple[List[str], int]:
        """
        Iterates through pagination pages for a single discovery seed URL.
        Stops early if a page fails to load or yields zero new links.
        """
        discovered: List[str] = []
        seen_on_this_seed = set()
        pages_crawled = 0

        base_parsed = urlparse(str(p_config.base_url))
        base_domain = f"{base_parsed.scheme}://{base_parsed.netloc}"
        wait_selector = self._resolve_primary_css_selector(p_config.selectors.job_links)

        for page_num in range(1, max_pages + 1):
            target_url = self._build_pagination_url(seed_url, page_num, platform=platform)
            seed_job_id = f"SEED-{platform.upper()}-S{seed_index}-P{page_num}"

            with log_context(job_id=seed_job_id, platform=platform, url=target_url):
                logger.info(f"Fetching discovery listing page {page_num}/{max_pages}: {target_url}")
                try:
                    response = self.transport.fetch(
                        url=target_url,
                        platform=platform,
                        job_id=seed_job_id,
                        requires_js=p_config.requires_js,
                        rate_limit_seconds=p_config.rate_limit_seconds,
                        wait_for_selector=wait_selector if p_config.requires_js else None,
                    )
                    pages_crawled += 1
                except TransportError as e:
                    logger.warning(
                        f"Transport failed on listing page {page_num} ({target_url}): {e}. "
                        f"Stopping pagination for this seed."
                    )
                    break
                except Exception as e:
                    logger.error(
                        f"Unexpected error fetching listing page {page_num} ({target_url}): {e}",
                        exc_info=True,
                    )
                    break

                page_links = self._extract_job_links_from_html(
                    raw_html=response.html,
                    base_domain=base_domain,
                    selectors=p_config.selectors.job_links,
                    log_ctx=f"[job_id={seed_job_id} | platform={platform}]",
                )

                # Filter out links already seen on previous pages of this same seed
                new_links_on_page = [u for u in page_links if u not in seen_on_this_seed]
                for u in new_links_on_page:
                    seen_on_this_seed.add(u)

                if not new_links_on_page:
                    logger.info(
                        f"Page {page_num} yielded 0 new job links (raw_extracted={len(page_links)}). "
                        f"Reached end of pagination for seed."
                    )
                    break

                discovered.extend(new_links_on_page)
                logger.info(
                    f"Extracted {len(new_links_on_page)} new job link(s) from page {page_num} "
                    f"(cumulative_for_seed={len(discovered)})."
                )

        return discovered, pages_crawled

    def _extract_job_links_from_html(
        self,
        raw_html: str,
        base_domain: str,
        selectors: Union[str, List[str]],
        log_ctx: str,
    ) -> List[str]:
        """
        Extracts and normalizes job advertisement URLs from listing HTML using
        configured CSS or XPath selectors while preserving top-to-bottom DOM order.
        """
        if not raw_html or not raw_html.strip():
            return []

        parser = "lxml" if LXML_AVAILABLE else "html.parser"
        soup = BeautifulSoup(raw_html, parser)
        dom_tree = None
        if LXML_AVAILABLE:
            try:
                dom_tree = etree.HTML(raw_html)
            except Exception as e:
                logger.debug(f"{log_ctx} lxml etree parse warning on listing page: {e}")

        selector_list = [selectors] if isinstance(selectors, str) else list(selectors)
        found_urls: List[str] = []
        base_netloc = urlparse(base_domain).netloc.lower().replace("www.", "")

        for sel in selector_list:
            sel = sel.strip()
            if not sel:
                continue

            raw_hrefs: List[str] = []
            try:
                # Branch 1: XPath Selector
                if sel.startswith("/") or sel.startswith("("):
                    if dom_tree is None:
                        logger.warning(
                            f"{log_ctx} XPath selector '{sel}' configured, but lxml is not installed."
                        )
                        continue
                    nodes = dom_tree.xpath(sel)
                    for node in nodes:
                        if isinstance(node, str):
                            raw_hrefs.append(node)
                        elif hasattr(node, "get") and node.get("href"):
                            raw_hrefs.append(str(node.get("href")))
                # Branch 2: CSS Selector
                else:
                    elements = soup.select(sel)
                    for el in elements:
                        href = el.get("href")
                        if href and isinstance(href, str):
                            raw_hrefs.append(href)
            except Exception as e:
                logger.warning(f"{log_ctx} Selector evaluation error for '{sel}': {e}")
                continue

            for href in raw_hrefs:
                candidate = self._clean_and_validate_candidate_url(href, base_domain, base_netloc)
                if candidate:
                    found_urls.append(candidate)

            # If primary selector matched valid links, we still check fallback selectors or deduplicate
            if found_urls:
                logger.debug(
                    f"{log_ctx} Selector '{sel}' matched {len(raw_hrefs)} raw href(s) "
                    f"({len(found_urls)} valid)."
                )

        # Preserve top-to-bottom discovery order while removing duplicates
        return list(dict.fromkeys(found_urls))

    @staticmethod
    def _clean_and_validate_candidate_url(
        raw_href: str,
        base_domain: str,
        base_netloc: str,
    ) -> Optional[str]:
        """
        Resolves relative URLs against `base_domain`, enforces same-domain security,
        and filters out non-job navigation links.
        """
        href_clean = raw_href.strip()
        if not href_clean or href_clean.startswith(("#", "javascript:", "mailto:", "tel:")):
            return None

        absolute_url = urljoin(base_domain, href_clean)
        parsed = urlparse(absolute_url)

        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None

        # Ensure link belongs to the target platform domain (prevent external ad/social links)
        candidate_netloc = parsed.netloc.lower().replace("www.", "")
        if candidate_netloc != base_netloc and not candidate_netloc.endswith(f".{base_netloc}"):
            return None

        path_lower = parsed.path.lower()
        if path_lower in ("", "/"):
            return None

        for ignored in IGNORED_PATH_SEGMENTS:
            if path_lower.startswith(ignored):
                return None

        return absolute_url

    @staticmethod
    def _build_pagination_url(seed_url: str, page_num: int, platform: str = "") -> str:
        """
        Constructs platform-appropriate pagination URLs for page_num >= 1.
        Supports WordPress `/page/N/` path pagination (IranEstekhdam) as well as
        standard `?page=N` query parameter pagination (Jobinja, JobVision, Quera).
        """
        if page_num <= 1:
            return seed_url

        parsed = urlparse(seed_url)

        # IranEstekhdam uses WordPress category archives (/category/.../page/2/)
        if platform.lower() == "iranestekhdam" and "/category/" in parsed.path:
            clean_path = re.sub(r"/page/\d+/?$", "", parsed.path.rstrip("/"))
            new_path = f"{clean_path}/page/{page_num}/"
            return urlunparse((
                parsed.scheme,
                parsed.netloc,
                new_path,
                parsed.params,
                parsed.query,
                "",
            ))

        # Standard query-string pagination (?page=N)
        query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query_items["page"] = str(page_num)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query_items, doseq=True),
            "",
        ))

    @staticmethod
    def _resolve_primary_css_selector(selectors: Union[str, List[str]]) -> Optional[str]:
        """Returns the first CSS selector (non-XPath) to pass to Playwright wait_for_selector."""
        sel_list = [selectors] if isinstance(selectors, str) else list(selectors)
        for s in sel_list:
            s_clean = s.strip()
            if s_clean and not s_clean.startswith("/") and not s_clean.startswith("("):
                return s_clean
        return None


# =====================================================================
# Standalone Verification Suite
# =====================================================================

if __name__ == "__main__":
    print("\n--- RUNNING URL_SEEDER.PY VERIFICATION SUITE ---\n")

    test_db = SCRAPER_ROOT_DIR / "logs" / "test_seeder_ledger.db"
    if test_db.exists():
        test_db.unlink()

    ledger = StateLedger(db_path=test_db)
    with URLSeeder(ledger=ledger) as seeder:

        # 1. Test Standard & WordPress Pagination Builders
        p1 = seeder._build_pagination_url("https://jobinja.ir/jobs/latest", 1, platform="jobinja")
        p2 = seeder._build_pagination_url("https://jobinja.ir/jobs/latest", 3, platform="jobinja")
        p3 = seeder._build_pagination_url("https://jobinja.ir/jobs/latest?q=python&page=1", 2, platform="jobinja")
        p4 = seeder._build_pagination_url(
            "https://iranestekhdam.ir/category/ai-jobs/", 2, platform="iranestekhdam"
        )
        assert p1 == "https://jobinja.ir/jobs/latest"
        assert p2 == "https://jobinja.ir/jobs/latest?page=3"
        assert p3 == "https://jobinja.ir/jobs/latest?q=python&page=2"
        assert p4 == "https://iranestekhdam.ir/category/ai-jobs/page/2/"
        print(
            f"✅ Test 1 (Pagination Builder) :\n"
            f"   -> Jobinja Page 3       : {p2}\n"
            f"   -> Existing Query Page 2: {p3}\n"
            f"   -> IranEstekhdam Page 2 : {p4}"
        )

        # 2. Test Link Extractor (CSS + XPath + Order Preservation + External/Ignored Filtering)
        sample_listing_html = """
        <html>
          <body>
            <a class="c-jobListView__titleLink" href="/companies/fanoos/jobs/ai-eng-1">AI Engineer 1</a>
            <a class="c-jobListView__titleLink" href="https://jobinja.ir/companies/fanoos/jobs/ai-eng-2">AI Engineer 2</a>
            <a class="c-jobListView__titleLink" href="/companies/fanoos/jobs/ai-eng-1">Duplicate AI Engineer 1</a>
            <a class="c-jobListView__titleLink" href="/login">Login Link (Should Ignore)</a>
            <a class="c-jobListView__titleLink" href="https://google.com/jobs/external">External Domain (Should Ignore)</a>
            <a href="#top">Back to top</a>
          </body>
        </html>
        """
        extracted_css = seeder._extract_job_links_from_html(
            raw_html=sample_listing_html,
            base_domain="https://jobinja.ir",
            selectors=["a.c-jobListView__titleLink"],
            log_ctx="[test=css_parser]",
        )
        assert extracted_css == [
            "https://jobinja.ir/companies/fanoos/jobs/ai-eng-1",
            "https://jobinja.ir/companies/fanoos/jobs/ai-eng-2",
        ]
        print(f"✅ Test 2 (CSS Link Extractor) : Extracted {len(extracted_css)} clean, ordered URLs.")

        # 3. Test Ledger Registration Integration
        reg_stats = ledger.register_urls(platform="jobinja", urls=extracted_css)
        assert reg_stats["inserted_new"] == 2
        print(f"✅ Test 3 (Ledger Integration) : {reg_stats}\n")

    # Clean up temporary SQLite database files
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{test_db}{suffix}")
        if p.exists():
            p.unlink()