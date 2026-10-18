"""
Module: cascade_extractor.py
Description: Implements a 3-tier resilient extraction cascade to isolate raw Persian job content from HTML.
Prioritizes Tier 1 (structured schema.org/JobPosting JSON-LD), falls back to Tier 2 (declarative 
CSS/XPath selectors from sites_config.yaml), and terminates at Tier 3 (Trafilatura heuristic main-content extraction).
Strictly extracts core raw primitives (raw_title, raw_company, raw_location, raw_text) without performing 
downstream entity parsing, guaranteeing uninterrupted extraction despite frontend DOM changes.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from bs4 import BeautifulSoup

try:
    from lxml import etree
    LXML_AVAILABLE = True
except ImportError:
    LXML_AVAILABLE = False

try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

try:
    from logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("cascade_extractor")


# =====================================================================
# Custom Exceptions for Scraper Control Flow
# =====================================================================

class ExtractionError(Exception):
    """Base exception for all extraction failures."""
    pass


class BotChallengeDetectedError(ExtractionError):
    """
    Raised when the HTML DOM contains a CAPTCHA, Cloudflare, or ArvanCloud WAF challenge.
    Signals scraper_service.py / transport.py to retry with stealth Playwright or rotate proxy.
    """
    def __init__(self, message: str, challenge_type: str, url: str = ""):
        super().__init__(message)
        self.challenge_type = challenge_type
        self.url = url


class UnrenderedSPAError(ExtractionError):
    """
    Raised when the HTML is an unhydrated JavaScript SPA shell (e.g., JobVision <app-root> or Quera #__next).
    Signals transport.py that curl_cffi is insufficient and headless browser rendering is required.
    """
    def __init__(self, message: str, url: str = ""):
        super().__init__(message)
        self.url = url


# =====================================================================
# Output Contract (Bridges to models.py)
# =====================================================================

@dataclass
class ExtractionResult:
    """Standardized output of the 3-tier extraction cascade."""
    raw_title: Optional[str] = None
    raw_company: Optional[str] = None
    raw_location: Optional[str] = None
    raw_text: Optional[str] = None
    extraction_tier_used: str = "NONE"
    tiers_attempted: List[str] = field(default_factory=list)
    extraction_duration_ms: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def is_complete(self, min_text_length: int = 80) -> bool:
        """Verifies whether we have both a title and a substantive raw job description."""
        has_title = bool(self.raw_title and len(self.raw_title.strip()) >= 2)
        has_text = bool(self.raw_text and len(self.raw_text.strip()) >= min_text_length)
        return has_title and has_text


# =====================================================================
# Core 3-Tier Cascade Extractor
# =====================================================================

class CascadeExtractor:
    """
    Resilient 3-tier HTML extractor tuned for Iranian job boards (Jobinja, JobVision,
    IranEstekhdam, Quera, Karboom) with pre-extraction CAPTCHA/WAF/SPA trap detection.
    """

    CAPTCHA_DOM_SELECTORS = [
        "div.g-recaptcha",
        "iframe[src*='recaptcha']",
        "div.cf-turnstile",
        "iframe[src*='challenges.cloudflare.com']",
        "div.h-captcha",
        "iframe[src*='hcaptcha.com']",
        "#challenge-running",
        "#cf-challenge-running",
        "form#challenge-form",
        "div#arvancloud-challenge",
    ]

    WAF_TEXT_SIGNATURES = [
        "لطفاً تیک من ربات نیستم",
        "لطفا کپچا را وارد کنید",
        "در حال بررسی امنیتی مرورگر",
        "دسترسی شما به این وب‌سایت مسدود شده",
        "حفاظت ابری آروان",
        "just a moment...",
        "attention required! | cloudflare",
        "please verify you are a human",
        "enable javascript and cookies to continue",
        "ddos protection by",
    ]

    SPA_ROOT_SELECTORS = [
        "app-root",      # Angular (JobVision)
        "div#__next",    # Next.js (Quera Magnet)
        "div#__nuxt",    # Nuxt.js
        "div#root",      # React
        "div#app",       # Vue.js
    ]

    def __init__(self, min_description_chars: int = 80):
        self.min_description_chars = min_description_chars
        logger.debug(
            f"Initialized CascadeExtractor (min_description_chars={self.min_description_chars}, "
            f"lxml={LXML_AVAILABLE}, trafilatura={TRAFILATURA_AVAILABLE})"
        )

    def extract(
        self,
        html: str,
        url: str = "",
        platform: str = "unknown",
        selectors: Optional[Dict[str, Union[str, List[str]]]] = None,
        job_id: str = "N/A"
    ) -> ExtractionResult:
        """
        Main entrypoint. Inspects HTML for anti-bot traps, then executes Tier 1 -> Tier 2 -> Tier 3.
        """
        start_time = time.perf_counter()
        log_ctx = f"[job_id={job_id} | platform={platform} | url={url}]"
        logger.info(f"{log_ctx} Starting extraction cascade (HTML size: {len(html or '')} bytes).")

        result = ExtractionResult()

        if not html or not html.strip():
            logger.error(f"{log_ctx} Received empty HTML payload.")
            raise ExtractionError(f"Empty HTML payload received for URL: {url}")

        parser = "lxml" if LXML_AVAILABLE else "html.parser"
        soup = BeautifulSoup(html, parser)

        # -----------------------------------------------------------------
        # STEP 0: Pre-Extraction Security & Trap Guard (CAPTCHA / WAF / SPA)
        # -----------------------------------------------------------------
        logger.debug(f"{log_ctx} [Step 0] Running DOM security & anti-bot inspection...")
        self._inspect_for_traps(soup, html, url, selectors, log_ctx)
        logger.debug(f"{log_ctx} [Step 0] Passed DOM trap inspection cleanly.")

        # -----------------------------------------------------------------
        # TIER 1: Structured Schema.org/JobPosting (JSON-LD)
        # -----------------------------------------------------------------
        result.tiers_attempted.append("TIER_1_JSON_LD")
        logger.info(f"{log_ctx} [Tier 1] Attempting schema.org/JobPosting JSON-LD extraction...")
        t1_data = self._extract_tier1_json_ld(soup, log_ctx)

        if t1_data:
            self._merge_fields(result, t1_data, tier_name="TIER_1_JSON_LD", log_ctx=log_ctx)
            # Note: If platform has a dedicated 'info_box' selector (like Jobinja/IranEstekhdam),
            # we still check Tier 2 to prepend the structured info_box tags onto raw_text!
            has_info_box_config = bool(selectors and selectors.get("info_box"))
            if result.is_complete(self.min_description_chars) and not has_info_box_config:
                result.extraction_duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
                logger.info(
                    f"{log_ctx} [Tier 1] SUCCESS. Full extraction completed in "
                    f"{result.extraction_duration_ms}ms (text_len={len(result.raw_text)})."
                )
                return result
            elif has_info_box_config:
                logger.info(
                    f"{log_ctx} [Tier 1] Extracted JSON-LD, continuing to Tier 2 to capture platform 'info_box' metadata."
                )
            else:
                msg = (
                    f"Tier 1 partial match (title={bool(result.raw_title)}, "
                    f"text_len={len(result.raw_text or '')}). Falling back to Tier 2."
                )
                result.warnings.append(msg)
                logger.warning(f"{log_ctx} [Tier 1] {msg}")
        else:
            logger.info(f"{log_ctx} [Tier 1] No valid JobPosting JSON-LD found. Proceeding to Tier 2.")

        # -----------------------------------------------------------------
        # TIER 2: Declarative CSS / XPath Selectors (from sites_config.yaml)
        # -----------------------------------------------------------------
        result.tiers_attempted.append("TIER_2_SELECTORS")
        if selectors:
            logger.info(f"{log_ctx} [Tier 2] Attempting declarative CSS/XPath selector extraction...")
            t2_data = self._extract_tier2_selectors(soup, html, selectors, log_ctx)
            if t2_data:
                self._merge_fields(result, t2_data, tier_name="TIER_2_SELECTORS", log_ctx=log_ctx)
                if result.is_complete(self.min_description_chars):
                    result.extraction_duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    logger.info(
                        f"{log_ctx} [Tier 2] SUCCESS. Extraction completed via selectors in "
                        f"{result.extraction_duration_ms}ms (text_len={len(result.raw_text)})."
                    )
                    return result
                else:
                    msg = (
                        f"Tier 2 partial match (title={bool(result.raw_title)}, "
                        f"text_len={len(result.raw_text or '')}). Falling back to Tier 3."
                    )
                    result.warnings.append(msg)
                    logger.warning(f"{log_ctx} [Tier 2] {msg}")
            else:
                logger.warning(f"{log_ctx} [Tier 2] All configured selectors returned empty. Proceeding to Tier 3.")
        else:
            logger.info(f"{log_ctx} [Tier 2] No platform selectors provided. Skipping directly to Tier 3.")

        # -----------------------------------------------------------------
        # TIER 3: Trafilatura & Heuristic Main-Content Extraction
        # -----------------------------------------------------------------
        result.tiers_attempted.append("TIER_3_HEURISTIC")
        logger.info(f"{log_ctx} [Tier 3] Attempting heuristic main-content extraction (Trafilatura/DOM)...")
        t3_data = self._extract_tier3_heuristic(soup, html, log_ctx)

        if t3_data:
            self._merge_fields(result, t3_data, tier_name="TIER_3_HEURISTIC", log_ctx=log_ctx)

        result.extraction_duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        if result.raw_text and len(result.raw_text.strip()) > 0:
            logger.info(
                f"{log_ctx} [Tier 3] Finished cascade via {result.extraction_tier_used} in "
                f"{result.extraction_duration_ms}ms (final text_len={len(result.raw_text)})."
            )
            return result

        logger.error(
            f"{log_ctx} Cascade exhausted all 3 tiers without extracting job text "
            f"(duration={result.extraction_duration_ms}ms)."
        )
        raise ExtractionError(f"All 3 extraction tiers failed to extract content for URL: {url}")

    # =================================================================
    # Step 0: Anti-Bot, CAPTCHA & Unrendered SPA Inspection
    # =================================================================

    def _inspect_for_traps(
        self,
        soup: BeautifulSoup,
        raw_html: str,
        url: str,
        selectors: Optional[Dict[str, Union[str, List[str]]]],
        log_ctx: str
    ) -> None:
        """
        Detects CAPTCHA widgets, WAF interstitial pages, and unhydrated JS SPA shells before extraction.
        """
        body_clone = BeautifulSoup(raw_html, "html.parser")
        for tag in body_clone(["script", "style", "noscript", "svg", "path"]):
            tag.decompose()
        visible_text = body_clone.get_text(separator=" ", strip=True)
        visible_text_lower = visible_text.lower()
        visible_len = len(visible_text)

        # 1. Check for explicit CAPTCHA / WAF Text Signatures on short pages (< 1500 chars)
        if visible_len < 1500:
            for signature in self.WAF_TEXT_SIGNATURES:
                if signature in visible_text_lower:
                    logger.error(
                        f"{log_ctx} [TRAP DETECTED] WAF/CAPTCHA text signature matched: '{signature}' "
                        f"(visible_len={visible_len})."
                    )
                    raise BotChallengeDetectedError(
                        message=f"WAF or CAPTCHA page detected via text signature: '{signature}'",
                        challenge_type="WAF_OR_CAPTCHA_TEXT",
                        url=url
                    )

        # 2. Check for active CAPTCHA DOM elements when main page content is sparse (< 800 chars)
        if visible_len < 800:
            for selector in self.CAPTCHA_DOM_SELECTORS:
                if soup.select_one(selector):
                    logger.error(
                        f"{log_ctx} [TRAP DETECTED] Blocking CAPTCHA widget found via selector '{selector}' "
                        f"on sparse page (visible_len={visible_len})."
                    )
                    raise BotChallengeDetectedError(
                        message=f"Blocking CAPTCHA widget detected ({selector})",
                        challenge_type="CAPTCHA_DOM_WIDGET",
                        url=url
                    )

        # 3. Check for unrendered Angular/Next.js/React SPA shells (e.g., JobVision <app-root>)
        for spa_selector in self.SPA_ROOT_SELECTORS:
            spa_node = soup.select_one(spa_selector)
            if spa_node:
                spa_inner_text = spa_node.get_text(separator=" ", strip=True)
                # Empty SPA mount node OR navbar-only shell where expected job description selector is absent
                if len(spa_inner_text) < 120:
                    logger.warning(
                        f"{log_ctx} [SPA SHELL DETECTED] Found '{spa_selector}' with only "
                        f"{len(spa_inner_text)} inner chars. Page requires Playwright JS rendering."
                    )
                    raise UnrenderedSPAError(
                        message=f"Unrendered SPA shell detected ({spa_selector}, inner_len={len(spa_inner_text)})",
                        url=url
                    )

    # =================================================================
    # Tier 1: Schema.org/JobPosting JSON-LD Parser
    # =================================================================

    def _extract_tier1_json_ld(self, soup: BeautifulSoup, log_ctx: str) -> Optional[Dict[str, Optional[str]]]:
        """Finds and parses <script type='application/ld+json'> blocks for JobPosting objects."""
        scripts = soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)})
        logger.debug(f"{log_ctx} [Tier 1] Found {len(scripts)} JSON-LD script block(s).")

        for idx, script in enumerate(scripts):
            raw_script = script.string or script.get_text()
            if not raw_script or not raw_script.strip():
                continue

            try:
                cleaned_json = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", raw_script.strip())
                payload = json.loads(cleaned_json)
            except json.JSONDecodeError as e:
                logger.debug(f"{log_ctx} [Tier 1] Script #{idx + 1} JSONDecodeError: {e}")
                continue

            job_posting = self._find_job_posting_node(payload)
            if not job_posting:
                continue

            logger.debug(f"{log_ctx} [Tier 1] Located valid @type='JobPosting' in JSON-LD script #{idx + 1}.")

            title = self._clean_html_fragment(job_posting.get("title") or job_posting.get("name"))
            company = self._extract_json_ld_company(job_posting.get("hiringOrganization"))
            location = self._extract_json_ld_location(job_posting.get("jobLocation"))
            description = self._clean_html_fragment(
                job_posting.get("description") or job_posting.get("responsibilities")
            )

            return {
                "raw_title": title,
                "raw_company": company,
                "raw_location": location,
                "raw_text": description,
            }

        return None

    def _find_job_posting_node(self, data: Any) -> Optional[Dict[str, Any]]:
        if isinstance(data, dict):
            node_type = data.get("@type")
            if node_type == "JobPosting" or (isinstance(node_type, list) and "JobPosting" in node_type):
                return data
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    found = self._find_job_posting_node(item)
                    if found:
                        return found
        elif isinstance(data, list):
            for item in data:
                found = self._find_job_posting_node(item)
                if found:
                    return found
        return None

    @staticmethod
    def _extract_json_ld_company(org_node: Any) -> Optional[str]:
        if isinstance(org_node, str):
            return org_node.strip()
        if isinstance(org_node, dict):
            name = org_node.get("name")
            return str(name).strip() if name else None
        return None

    @staticmethod
    def _extract_json_ld_location(loc_node: Any) -> Optional[str]:
        if isinstance(loc_node, str):
            return loc_node.strip()
        if isinstance(loc_node, list) and loc_node:
            loc_node = loc_node[0]
        if isinstance(loc_node, dict):
            address = loc_node.get("address")
            if isinstance(address, str):
                return address.strip()
            if isinstance(address, dict):
                parts = [address.get("addressLocality"), address.get("addressRegion")]
                valid_parts = [str(p).strip() for p in parts if p and str(p).strip()]
                return "، ".join(valid_parts) if valid_parts else None
        return None

    # =================================================================
    # Tier 2: Declarative CSS / XPath Selector Extraction
    # =================================================================

    def _extract_tier2_selectors(
        self,
        soup: BeautifulSoup,
        raw_html: str,
        selectors: Dict[str, Union[str, List[str]]],
        log_ctx: str
    ) -> Optional[Dict[str, Optional[str]]]:
        """
        Applies declarative CSS/XPath selectors from sites_config.yaml.
        Combines 'info_box' (top metadata table on Jobinja/IranEstekhdam) with 'description'
        so downstream agents receive 100% of the job specifications.
        """
        dom_tree = None
        if LXML_AVAILABLE:
            try:
                dom_tree = etree.HTML(raw_html)
            except Exception as e:
                logger.debug(f"{log_ctx} [Tier 2] lxml etree parse warning: {e}")

        title = self._query_field(soup, dom_tree, selectors.get("title"), "title", log_ctx)
        company = self._query_field(soup, dom_tree, selectors.get("company"), "company", log_ctx)
        location = self._query_field(soup, dom_tree, selectors.get("location"), "location", log_ctx)

        # Extract both the top info_box (salary, contract type, experience, skills) and main prose description
        info_box_text = self._query_field(
            soup, dom_tree, selectors.get("info_box"), "info_box", log_ctx, join_multiple=True
        )
        desc_text = self._query_field(
            soup,
            dom_tree,
            selectors.get("description") or selectors.get("text"),
            "description",
            log_ctx,
            join_multiple=True
        )

        combined_text_parts = []
        if info_box_text:
            combined_text_parts.append(f"[مشخصات و تگ‌های آگهی]\n{info_box_text}")
        if desc_text:
            combined_text_parts.append(f"[شرح کامل موقعیت شغلی]\n{desc_text}" if info_box_text else desc_text)

        full_text = "\n\n".join(combined_text_parts) if combined_text_parts else None

        extracted = {
            "raw_title": title,
            "raw_company": company,
            "raw_location": location,
            "raw_text": full_text,
        }

        if any(extracted.values()):
            return extracted
        return None

    def _query_field(
        self,
        soup: BeautifulSoup,
        dom_tree: Any,
        selector_config: Optional[Union[str, List[str]]],
        field_name: str,
        log_ctx: str,
        join_multiple: bool = False
    ) -> Optional[str]:
        if not selector_config:
            return None

        selector_list = [selector_config] if isinstance(selector_config, str) else selector_config

        for sel in selector_list:
            sel = sel.strip()
            if not sel:
                continue

            try:
                if sel.startswith("/") or sel.startswith("("):
                    if dom_tree is None:
                        logger.warning(
                            f"{log_ctx} [Tier 2] XPath selector '{sel}' requested for '{field_name}', "
                            f"but lxml is not installed."
                        )
                        continue
                    nodes = dom_tree.xpath(sel)
                    if nodes:
                        texts = [
                            n.strip() if isinstance(n, str) else " ".join(n.itertext()).strip()
                            for n in nodes
                        ]
                        texts = [t for t in texts if t]
                        if texts:
                            val = "\n".join(texts) if join_multiple else texts[0]
                            logger.debug(
                                f"{log_ctx} [Tier 2] XPath matched field '{field_name}' via '{sel}' (len={len(val)})."
                            )
                            return val
                else:
                    elements = soup.select(sel)
                    if elements:
                        if join_multiple:
                            texts = [el.get_text(separator="\n", strip=True) for el in elements]
                            val = "\n\n".join([t for t in texts if t])
                        else:
                            val = elements[0].get_text(separator=" ", strip=True)
                        if val:
                            logger.debug(
                                f"{log_ctx} [Tier 2] CSS matched field '{field_name}' via '{sel}' (len={len(val)})."
                            )
                            return val
            except Exception as e:
                logger.warning(f"{log_ctx} [Tier 2] Selector error on '{sel}' for '{field_name}': {e}")

        logger.debug(f"{log_ctx} [Tier 2] No match for field '{field_name}' across {len(selector_list)} selector(s).")
        return None

    # =================================================================
    # Tier 3: Trafilatura & Heuristic Main-Content Fallback
    # =================================================================

    def _extract_tier3_heuristic(
        self,
        soup: BeautifulSoup,
        raw_html: str,
        log_ctx: str
    ) -> Optional[Dict[str, Optional[str]]]:
        extracted_title: Optional[str] = None
        extracted_text: Optional[str] = None

        h1_tag = soup.find("h1")
        if h1_tag:
            extracted_title = h1_tag.get_text(separator=" ", strip=True)
        else:
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                extracted_title = str(og_title["content"]).strip()
            elif soup.title and soup.title.string:
                extracted_title = soup.title.string.strip()

        if TRAFILATURA_AVAILABLE:
            try:
                logger.debug(f"{log_ctx} [Tier 3] Running Trafilatura main-content extraction...")
                extracted_text = trafilatura.extract(
                    raw_html,
                    include_comments=False,
                    include_tables=True,
                    no_fallback=False,
                    favor_recall=True,
                )
                if extracted_text:
                    logger.debug(f"{log_ctx} [Tier 3] Trafilatura extracted {len(extracted_text)} characters.")
            except Exception as e:
                logger.warning(f"{log_ctx} [Tier 3] Trafilatura raised an exception: {e}")

        if not extracted_text or len(extracted_text.strip()) < self.min_description_chars:
            logger.debug(f"{log_ctx} [Tier 3] Running semantic DOM pruning fallback (<main>/<article>/<body>)...")
            fallback_soup = BeautifulSoup(raw_html, "html.parser")
            for noise_tag in fallback_soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "form"]):
                noise_tag.decompose()

            main_container = (
                fallback_soup.find("main")
                or fallback_soup.find("article")
                or fallback_soup.find("div", attrs={"role": "main"})
                or fallback_soup.body
                or fallback_soup
            )
            if main_container:
                extracted_text = main_container.get_text(separator="\n", strip=True)

        if extracted_title or extracted_text:
            return {
                "raw_title": extracted_title,
                "raw_company": None,
                "raw_location": None,
                "raw_text": extracted_text,
            }
        return None

    # =================================================================
    # Utility & Cross-Tier Field Merger
    # =================================================================

    def _merge_fields(
        self,
        result: ExtractionResult,
        new_data: Dict[str, Optional[str]],
        tier_name: str,
        log_ctx: str
    ) -> None:
        updated_any = False

        for field_key in ("raw_title", "raw_company", "raw_location"):
            current_val = getattr(result, field_key)
            candidate_val = new_data.get(field_key)
            if not current_val and candidate_val and candidate_val.strip():
                setattr(result, field_key, candidate_val.strip())
                updated_any = True

        current_text = result.raw_text or ""
        candidate_text = (new_data.get("raw_text") or "").strip()
        if candidate_text:
            # Upgrade if current text is shorter than threshold OR if Tier 2 enriched text with [مشخصات و تگ‌های آگهی]
            if len(current_text) < self.min_description_chars or (
                "[مشخصات و تگ‌های آگهی]" in candidate_text and "[مشخصات و تگ‌های آگهی]" not in current_text
            ):
                result.raw_text = candidate_text
                result.extraction_tier_used = tier_name
                updated_any = True

        if updated_any and result.extraction_tier_used == "NONE":
            result.extraction_tier_used = tier_name

        logger.debug(
            f"{log_ctx} [{tier_name}] Merge state -> tier_used={result.extraction_tier_used}, "
            f"title={bool(result.raw_title)}, company={bool(result.raw_company)}, "
            f"location={bool(result.raw_location)}, text_len={len(result.raw_text or '')}"
        )

    @staticmethod
    def _clean_html_fragment(raw_fragment: Optional[str]) -> Optional[str]:
        if not raw_fragment or not isinstance(raw_fragment, str):
            return None
        frag_soup = BeautifulSoup(raw_fragment, "html.parser")
        cleaned = frag_soup.get_text(separator="\n", strip=True)
        return cleaned if cleaned else None


if __name__ == "__main__":
    print("\n--- RUNNING IRANIAN JOB MARKET CASCADE EXTRACTOR TEST ---\n")
    extractor = CascadeExtractor(min_description_chars=40)

    # Simulated Jobinja page with separate .c-infoBox (Salary/Skills) + .s-jobDesc (Description)
    jobinja_html = """
    <html>
      <body>
        <div class="c-jobView__titleText"><h1>استخدام برنامه‌نویس ارشد پایتون (Senior Python Developer)</h1></div>
        <h2 class="c-companyHeader__title">شرکت داده‌پردازی فانوس</h2>
        <ul class="c-infoBox">
          <li class="c-infoBox__item"><h4>دسته‌بندی شغلی</h4><span>وب، برنامه‌نویسی و نرم‌افزار</span></li>
          <li class="c-infoBox__item"><h4>محل کار</h4><span>تهران، ونک</span></li>
          <li class="c-infoBox__item"><h4>نوع همکاری</h4><span>تمام وقت</span></li>
          <li class="c-infoBox__item"><h4>حداقل سابقه کار</h4><span>سه سال</span></li>
          <li class="c-infoBox__item"><h4>حقوق</h4><span>از ۴۵,۰۰۰,۰۰۰ تومان</span></li>
          <li class="c-infoBox__item"><h4>مهارت‌های مورد نیاز</h4><span>Python, FastAPI, Docker, Qdrant</span></li>
        </ul>
        <div class="s-jobDesc">
          ما در تیم فنی فانوس به دنبال یک مهندس بک‌اند مسلط به پایتون و معماری میکروسرویس هستیم.
        </div>
      </body>
    </html>
    """
    jobinja_selectors = {
        "title": "div.c-jobView__titleText h1",
        "company": "h2.c-companyHeader__title",
        "location": "ul.c-infoBox li:nth-of-type(2) span",
        "info_box": "ul.c-infoBox li",
        "description": "div.s-jobDesc"
    }
    res = extractor.extract(jobinja_html, url="https://jobinja.ir/jobs/1", platform="jobinja", selectors=jobinja_selectors)
    print(f"✅ Tier Used: {res.extraction_tier_used}\n✅ Title: {res.raw_title}\n✅ Combined Raw Text for Mani's Agent:\n{res.raw_text}\n")