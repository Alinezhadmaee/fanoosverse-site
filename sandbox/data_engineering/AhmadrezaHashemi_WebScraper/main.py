"""
Module: main.py
Description: CLI entrypoint and standalone batch runner for the Fanoos web scraping pipeline.
Orchestrates end-to-end execution by triggering URL seeding (url_seeder.py), iteratively 
invoking scraper_service.py to lease and process pending job ads, and exporting validated, 
sanitized raw Persian job records to a consolidated JSONL artifact for downstream agent consumption.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from config import get_config
from logger import configure_logging, get_logger
from models import JobLedgerStatus, RawJobAd, TransportEngine, TransportResponse
from scraper_service import ScraperService
from state_ledger import StateLedger
from transport import ResilientTransport

logger = get_logger(__name__)

SCRAPER_ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_JSONL = SCRAPER_ROOT_DIR / "output" / "raw_persian_jobs.jsonl"


# =====================================================================
# JSONL Artifact Exporter (With Deduplication)
# =====================================================================

def load_existing_jsonl_job_ids(output_path: Path) -> Set[str]:
    """
    Reads an existing JSONL output file and returns the set of `job_id` values
    already present to prevent duplicate entries across incremental batch runs.
    """
    existing_ids: Set[str] = set()
    if not output_path.exists():
        return existing_ids

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    job_id = data.get("job_id")
                    if job_id and isinstance(job_id, str):
                        existing_ids.add(job_id)
                except json.JSONDecodeError:
                    logger.warning(
                        f"Skipping malformed JSON line {line_num} while scanning '{output_path}'."
                    )
    except Exception as e:
        logger.warning(f"Could not scan existing JSONL file '{output_path}': {e}")

    return existing_ids


def export_ads_to_jsonl(
    ads: List[RawJobAd],
    output_path: Path,
    append: bool = True,
) -> Dict[str, int]:
    """
    Serializes a list of validated `RawJobAd` contracts into a UTF-8 `.jsonl` file.
    Guarantees zero duplicate `job_id` lines when `append=True`.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_ids = load_existing_jsonl_job_ids(output_path) if append else set()

    mode = "a" if append else "w"
    written_count = 0
    skipped_duplicates = 0

    with open(output_path, mode, encoding="utf-8") as f:
        for ad in ads:
            if ad.job_id in existing_ids:
                skipped_duplicates += 1
                continue
            f.write(ad.to_jsonl_line() + "\n")
            existing_ids.add(ad.job_id)
            written_count += 1

    logger.info(
        f"JSONL export complete -> file='{output_path}', "
        f"written_new={written_count}, skipped_duplicates={skipped_duplicates}, "
        f"total_in_file={len(existing_ids)}."
    )
    return {
        "written_new": written_count,
        "skipped_duplicates": skipped_duplicates,
        "total_in_file": len(existing_ids),
    }


# =====================================================================
# CLI Argument Parser
# =====================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fanoos-scraper",
        description="Fanoos Web Scraping Pipeline CLI & Batch JSONL Runner",
    )
    parser.add_argument(
        "--platform",
        type=str,
        default=None,
        help="Target a specific platform from sites_config.yaml (e.g., jobinja, iranestekhdam, jobvision, quera). Defaults to all enabled platforms.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Run the URL discovery seeder before leasing and scraping job ads.",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Only run the URL discovery seeder to populate scraper_ledger.db, then exit.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Override max_pages_per_seed during URL discovery.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of valid RawJobAd records to scrape in this batch (default: 20).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_JSONL,
        help=f"Path to the output JSONL file (default: {DEFAULT_OUTPUT_JSONL}).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output JSONL file instead of appending to it.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current SQLite state ledger statistics and exit.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override the console logging level.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the offline end-to-end CLI and JSONL export verification suite.",
    )
    return parser


# =====================================================================
# Main CLI Execution Flow
# =====================================================================

def run_cli(args: argparse.Namespace) -> int:
    """Executes the CLI workflow based on parsed command-line arguments."""
    config = get_config()
    configure_logging(
        log_level=args.log_level or config.env.log_level,
        log_dir=config.env.log_dir,
    )

    start_time = time.perf_counter()
    logger.info(
        f"Starting Fanoos Scraper CLI (env={config.env.environment}, "
        f"platform={args.platform or 'ALL'}, seed={args.seed or args.seed_only}, limit={args.limit})."
    )

    with ScraperService(config=config) as service:
        # 1. Status Inspection Mode (--status)
        if args.status:
            status_snapshot = service.get_pipeline_status()
            ledger_stats = status_snapshot["ledger_stats"]
            logger.info(f"Ledger Status Breakdown: {ledger_stats['by_status']}")
            print("\n=== FANOOS SCRAPER LEDGER STATUS ===")
            print(json.dumps(ledger_stats, indent=2))
            return 0

        # 2. Discovery / URL Seeding Phase (--seed or --seed-only)
        if args.seed or args.seed_only:
            seed_results = service.seed_urls(
                platform=args.platform,
                max_pages_override=args.max_pages,
            )
            logger.info(f"URL Seeding Summary: {seed_results}")
            if args.seed_only:
                print("\n=== URL SEEDING COMPLETE ===")
                print(json.dumps(seed_results, indent=2))
                return 0

        # 3. Batch Leasing, Scraping, Sanitization & Pre-Flight Validation Phase
        scraped_ads = service.scrape_batch(
            limit=args.limit,
            platform=args.platform,
            auto_ack=True,
        )

        # 4. Export Validated RawJobAd Contracts to JSONL
        export_stats = {"written_new": 0, "skipped_duplicates": 0, "total_in_file": 0}
        if scraped_ads:
            export_stats = export_ads_to_jsonl(
                ads=scraped_ads,
                output_path=args.output,
                append=not args.overwrite,
            )
        else:
            logger.warning(
                "No new RawJobAd records were collected in this batch. "
                "Tip: Run with '--seed' first if scraper_ledger.db has no PENDING URLs."
            )

        elapsed_sec = round(time.perf_counter() - start_time, 2)
        final_status = service.get_pipeline_status()

        logger.info(
            f"CLI batch execution finished in {elapsed_sec}s -> "
            f"scraped_valid={len(scraped_ads)}, written_new={export_stats['written_new']}, "
            f"session_metrics={final_status['session_metrics']}."
        )

        print("\n=== FANOOS SCRAPER RUN SUMMARY ===")
        print(f"Duration           : {elapsed_sec}s")
        print(f"Valid Ads Scraped  : {len(scraped_ads)}")
        print(f"JSONL File Path    : {args.output}")
        print(f"JSONL Export Stats : {export_stats}")
        print(f"Session Metrics    : {final_status['session_metrics']}")
        print(f"Ledger Totals      : {final_status['ledger_stats']['by_status']}\n")

    return 0


# =====================================================================
# Standalone Verification Suite (Runs when invoked with no args or --self-test)
# =====================================================================

def run_self_test() -> None:
    """
    Verifies end-to-end CLI orchestration, batch scraping, and deduplicated JSONL
    artifact generation using an isolated test database and output file.
    """
    configure_logging(log_level="DEBUG")
    print("\n--- RUNNING MAIN.PY END-TO-END CLI & JSONL EXPORT VERIFICATION SUITE ---\n")

    test_db_path = SCRAPER_ROOT_DIR / "logs" / "test_main_ledger.db"
    test_jsonl_path = SCRAPER_ROOT_DIR / "logs" / "test_output_jobs.jsonl"

    for p in (test_db_path, test_jsonl_path):
        if p.exists():
            p.unlink()

    class MockCLITransport(ResilientTransport):
        def fetch(
            self,
            url: str,
            platform: str = "unknown",
            job_id: str = "N/A",
            requires_js: bool = False,
            rate_limit_seconds: float = 0.0,
            wait_for_selector: Optional[str] = None,
        ) -> TransportResponse:
            html = """
            <html><body>
              <div class="c-jobView__titleText"><h1>استخدام مهندس یادگیری ماشین (ML Engineer)</h1></div>
              <h2 class="c-companyHeader__title">گروه هوش مصنوعی فانوس</h2>
              <ul class="c-infoBox">
                <li class="c-infoBox__item"><h4>محل کار</h4><span>تهران</span></li>
                <li class="c-infoBox__item"><h4>مهارت‌ها</h4><span>Python, PyTorch, Qdrant, Docker</span></li>
              </ul>
              <div class="s-jobDesc">
                ما در تیم فنی فانوس به دنبال یک مهندس یادگیری ماشین مسلط بهPythonوPyTorchبا حداقل۲سال تجربه کاری هستیم.
              </div>
            </body></html>
            """
            return TransportResponse(
                url=url,
                final_url=url,
                status_code=200,
                html=html,
                engine_used=TransportEngine.CURL_CFFI,
                elapsed_ms=28.4,
            )

    ledger = StateLedger(db_path=test_db_path)
    mock_transport = MockCLITransport()

    with ScraperService(ledger=ledger, transport=mock_transport) as service:
        # 1. Seed 2 URLs into test ledger
        ledger.register_urls(
            platform="jobinja",
            urls=[
                "https://jobinja.ir/companies/fanoos/jobs/ml-eng-1",
                "https://jobinja.ir/companies/fanoos/jobs/ml-eng-2",
            ],
        )

        # 2. Run batch scrape
        scraped_ads = service.scrape_batch(limit=5, platform="jobinja", auto_ack=True)
        assert len(scraped_ads) == 2

        # 3. Export to JSONL (First Write)
        stats_first = export_ads_to_jsonl(scraped_ads, output_path=test_jsonl_path, append=True)
        assert stats_first["written_new"] == 2
        assert stats_first["skipped_duplicates"] == 0
        print(f"✅ Test 1 (Initial JSONL Export)     : {stats_first}")

        # 4. Export same ads again to verify JSONL deduplication guard
        stats_second = export_ads_to_jsonl(scraped_ads, output_path=test_jsonl_path, append=True)
        assert stats_second["written_new"] == 0
        assert stats_second["skipped_duplicates"] == 2
        assert stats_second["total_in_file"] == 2
        print(f"✅ Test 2 (Idempotent JSONL Dedup)   : {stats_second}")

        # 5. Verify exported JSONL file content
        lines = test_jsonl_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        parsed_first = json.loads(lines[0])
        assert parsed_first["platform"] == "jobinja"
        assert "Python" in parsed_first["raw_text"]
        print(f"✅ Test 3 (JSONL Artifact Verified)  : {len(lines)} valid UTF-8 JSONL lines written.")

    # Cleanup temporary test files
    if test_jsonl_path.exists():
        test_jsonl_path.unlink()
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{test_db_path}{suffix}")
        if p.exists():
            p.unlink()

    print("\n=== CLI USAGE CHEAT SHEET (FOR LIVE RUNS) ===")
    print("1. Seed job URLs from Jobinja (1 page) and scrape 5 ads to JSONL:")
    print("   python3 main.py --platform jobinja --seed --max-pages 1 --limit 5")
    print("2. Seed all enabled platforms without scraping yet:")
    print("   python3 main.py --seed-only")
    print("3. View SQLite State Ledger telemetry:")
    print("   python3 main.py --status\n")


if __name__ == "__main__":
    cli_parser = build_arg_parser()
    # If invoked with no CLI flags (or --self-test), run the verification suite
    if len(sys.argv) == 1 or "--self-test" in sys.argv:
        run_self_test()
    else:
        parsed_args = cli_parser.parse_args()
        sys.exit(run_cli(parsed_args))

        