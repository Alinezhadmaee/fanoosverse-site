"""
Module: state_ledger.py
Description: Manages the local SQLite discovery ledger (scraper_ledger.db). Enforces ACID 
guarantees for single-item leasing, deduplication via SHA-256 URL hashing, lease timeouts 
for stalled jobs, and transactional status updates (ACK / NACK) driven by downstream agent results.
"""
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

from models import (
    JobLedgerStatus,
    LedgerJobRecord,
    compute_url_sha256,
    generate_deterministic_job_id,
    normalize_job_url,
)

try:
    from logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("state_ledger")


# =====================================================================
# Default Paths & SQLite Schema Definitions
# =====================================================================

SCRAPER_ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_LEDGER_DB_PATH = SCRAPER_ROOT_DIR / "scraper_ledger.db"

CREATE_LEDGER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job_ledger (
    job_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    retry_count INTEGER NOT NULL DEFAULT 0,
    leased_until TEXT DEFAULT NULL,
    last_error TEXT DEFAULT NULL,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_job_ledger_status_retry ON job_ledger (status, retry_count, discovered_at);",
    "CREATE INDEX IF NOT EXISTS idx_job_ledger_platform_status ON job_ledger (platform, status);",
    "CREATE INDEX IF NOT EXISTS idx_job_ledger_leased_until ON job_ledger (status, leased_until);",
]


# =====================================================================
# Core SQLite State Ledger
# =====================================================================

class StateLedger:
    """
    ACID-compliant SQLite state manager for URL discovery deduplication,
    single-item leasing with timeout recovery, and ACK/NACK lifecycle tracking.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        lease_timeout_seconds: int = 300,
        max_retries: int = 3,
        busy_timeout_ms: int = 10000,
    ):
        self.db_path = Path(db_path) if db_path else DEFAULT_LEDGER_DB_PATH
        self.lease_timeout_seconds = lease_timeout_seconds
        self.max_retries = max_retries
        self.busy_timeout_ms = busy_timeout_ms

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()
        logger.info(
            f"Initialized StateLedger (db='{self.db_path}', "
            f"lease_timeout={self.lease_timeout_seconds}s, max_retries={self.max_retries})."
        )

    # =================================================================
    # Connection & Transaction Management
    # =================================================================

    @contextmanager
    def _get_connection(self, immediate: bool = False) -> Generator[sqlite3.Connection, None, None]:
        """
        Opens a configured SQLite connection with WAL mode and Row factory.
        When `immediate=True`, begins an immediate write lock (`BEGIN IMMEDIATE`)
        to guarantee atomic read-modify-write leasing across processes.
        """
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # Autocommit mode so we control BEGIN/COMMIT explicitly
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms};")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA foreign_keys = ON;")

            if immediate:
                conn.execute("BEGIN IMMEDIATE;")
            else:
                conn.execute("BEGIN;")

            yield conn
            conn.execute("COMMIT;")
        except Exception as e:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            logger.error(f"SQLite transaction rolled back due to error: {e}")
            raise
        finally:
            conn.close()

    def _initialize_database(self) -> None:
        """Creates the `job_ledger` table and performance indexes if they do not exist."""
        with self._get_connection(immediate=True) as conn:
            conn.execute(CREATE_LEDGER_TABLE_SQL)
            for idx_sql in CREATE_INDEXES_SQL:
                conn.execute(idx_sql)
        logger.debug(f"Verified SQLite schema and indexes in '{self.db_path}'.")

    # =================================================================
    # 1. URL Seeding & Deduplication (Used by url_seeder.py)
    # =================================================================

    def register_urls(self, platform: str, urls: Iterable[str]) -> Dict[str, int]:
        """
        Normalizes, hashes, and bulk-inserts discovered job URLs into the ledger as PENDING.
        Automatically deduplicates against existing `url_hash` entries via `INSERT OR IGNORE`.
        Returns telemetry counts: `{"total_input": X, "inserted_new": Y, "duplicates_skipped": Z, "invalid_urls": W}`.
        """
        start_time = time.perf_counter()
        clean_platform = platform.lower().strip()
        now_iso = datetime.now(timezone.utc).isoformat()

        rows_to_insert: List[Tuple[str, str, str, str, str, int, str, str]] = []
        seen_hashes_in_batch = set()
        invalid_count = 0
        total_input = 0

        for raw_url in urls:
            total_input += 1
            try:
                canonical_url = normalize_job_url(raw_url)
                url_hash = compute_url_sha256(canonical_url)
                if url_hash in seen_hashes_in_batch:
                    continue
                seen_hashes_in_batch.add(url_hash)
                job_id = generate_deterministic_job_id(clean_platform, canonical_url)
                rows_to_insert.append((
                    job_id,
                    canonical_url,
                    url_hash,
                    clean_platform,
                    JobLedgerStatus.PENDING.value,
                    0,
                    now_iso,
                    now_iso,
                ))
            except ValueError as e:
                invalid_count += 1
                logger.warning(f"[platform={clean_platform}] Skipping malformed URL during seeding '{raw_url}': {e}")

        inserted_new = 0
        if rows_to_insert:
            with self._get_connection(immediate=True) as conn:
                before_changes = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO job_ledger (
                        job_id, url, url_hash, platform, status, retry_count, discovered_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    rows_to_insert,
                )
                inserted_new = conn.total_changes - before_changes

        duplicates_skipped = (total_input - invalid_count) - inserted_new
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        logger.info(
            f"[platform={clean_platform}] URL Seeding batch complete in {elapsed_ms}ms -> "
            f"input={total_input}, inserted_new={inserted_new}, "
            f"duplicates_skipped={duplicates_skipped}, invalid={invalid_count}."
        )
        return {
            "total_input": total_input,
            "inserted_new": inserted_new,
            "duplicates_skipped": duplicates_skipped,
            "invalid_urls": invalid_count,
        }

    # =================================================================
    # 2. Stalled Lease Reclamation & Atomic Single-Item Leasing
    # =================================================================

    def reclaim_expired_leases(self) -> int:
        """
        Sweeps jobs stuck in `LEASED` status past their `leased_until` timestamp.
        Transitions them to `FAILED_RETRYABLE` (if retries remain) or `DEAD_LETTER` (if exhausted).
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        reclaimed_count = 0

        with self._get_connection(immediate=True) as conn:
            expired_rows = conn.execute(
                """
                SELECT job_id, platform, retry_count, leased_until
                FROM job_ledger
                WHERE status = ? AND leased_until IS NOT NULL AND leased_until < ?;
                """,
                (JobLedgerStatus.LEASED.value, now_iso),
            ).fetchall()

            for row in expired_rows:
                job_id = row["job_id"]
                platform = row["platform"]
                new_retry_count = int(row["retry_count"]) + 1
                timeout_error = f"Lease expired at {row['leased_until']} without ACK/NACK."

                if new_retry_count >= self.max_retries:
                    next_status = JobLedgerStatus.DEAD_LETTER.value
                    logger.error(
                        f"[job_id={job_id} | platform={platform}] Stalled lease expired "
                        f"(retry {new_retry_count}/{self.max_retries}). Moving to DEAD_LETTER."
                    )
                else:
                    next_status = JobLedgerStatus.FAILED_RETRYABLE.value
                    logger.warning(
                        f"[job_id={job_id} | platform={platform}] Stalled lease expired. "
                        f"Reclaiming as FAILED_RETRYABLE (retry {new_retry_count}/{self.max_retries})."
                    )

                conn.execute(
                    """
                    UPDATE job_ledger
                    SET status = ?,
                        retry_count = ?,
                        leased_until = NULL,
                        last_error = ?,
                        updated_at = ?
                    WHERE job_id = ?;
                    """,
                    (next_status, new_retry_count, timeout_error, now_iso, job_id),
                )
                reclaimed_count += 1

        if reclaimed_count > 0:
            logger.info(f"Reclaimed {reclaimed_count} expired job lease(s).")
        return reclaimed_count

    def lease_next_job(self, platform: Optional[str] = None) -> Optional[LedgerJobRecord]:
        """
        Atomically selects and locks the next available job URL (`PENDING` first, then `FAILED_RETRYABLE`).
        Sets `status = LEASED` and `leased_until = now + lease_timeout_seconds`.
        Returns a validated `LedgerJobRecord` or `None` if the queue is empty.
        """
        # First reclaim any stalled leases that timed out
        self.reclaim_expired_leases()

        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        lease_expiry_iso = (now_dt + timedelta(seconds=self.lease_timeout_seconds)).isoformat()

        with self._get_connection(immediate=True) as conn:
            query = """
                SELECT *
                FROM job_ledger
                WHERE status IN (?, ?)
                  AND retry_count < ?
            """
            params: List[Any] = [
                JobLedgerStatus.PENDING.value,
                JobLedgerStatus.FAILED_RETRYABLE.value,
                self.max_retries,
            ]

            if platform:
                query += " AND platform = ?"
                params.append(platform.lower().strip())

            # Prioritize fresh PENDING jobs over retries, then FIFO by discovery timestamp
            query += """
                ORDER BY
                    CASE WHEN status = 'PENDING' THEN 0 ELSE 1 END ASC,
                    discovered_at ASC
                LIMIT 1;
            """

            row = conn.execute(query, params).fetchone()
            if not row:
                logger.debug(
                    f"No eligible PENDING or FAILED_RETRYABLE jobs available to lease "
                    f"(platform_filter={platform or 'ALL'})."
                )
                return None

            job_id = row["job_id"]
            conn.execute(
                """
                UPDATE job_ledger
                SET status = ?,
                    leased_until = ?,
                    updated_at = ?
                WHERE job_id = ?;
                """,
                (JobLedgerStatus.LEASED.value, lease_expiry_iso, now_iso, job_id),
            )

            updated_row = conn.execute(
                "SELECT * FROM job_ledger WHERE job_id = ?;", (job_id,)
            ).fetchone()

        record = self._row_to_model(updated_row)
        logger.info(
            f"[job_id={record.job_id} | platform={record.platform}] Leased job URL "
            f"(retry_count={record.retry_count}, leased_until={lease_expiry_iso})."
        )
        return record

    # =================================================================
    # 3. Transactional Lifecycle Updates (ACK / NACK / REJECT)
    # =================================================================

    def ack_job(self, job_id: str) -> bool:
        """
        Marks a leased job as `COMPLETED` and clears its lease timer.
        Called after downstream extraction, sanitization, and pre-flight validation succeed.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE job_ledger
                SET status = ?,
                    leased_until = NULL,
                    last_error = NULL,
                    updated_at = ?
                WHERE job_id = ?;
                """,
                (JobLedgerStatus.COMPLETED.value, now_iso, job_id),
            )
            updated = cursor.rowcount > 0

        if updated:
            logger.info(f"[job_id={job_id}] Transactional ACK committed -> status=COMPLETED.")
        else:
            logger.warning(f"[job_id={job_id}] ACK called on unknown job_id.")
        return updated

    def nack_job(self, job_id: str, error_message: str, retryable: bool = True) -> JobLedgerStatus:
        """
        Records a scraping or transport failure for `job_id`.
        Increments `retry_count` and transitions the record to `FAILED_RETRYABLE`
        (if `retryable=True` and `retry_count < max_retries`) or `DEAD_LETTER`.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_err = (error_message or "Unspecified error").strip()[:1000]

        with self._get_connection(immediate=True) as conn:
            row = conn.execute(
                "SELECT platform, retry_count FROM job_ledger WHERE job_id = ?;", (job_id,)
            ).fetchone()

            if not row:
                logger.warning(f"[job_id={job_id}] NACK called on non-existent job_id.")
                raise KeyError(f"Job ID '{job_id}' not found in state_ledger.")

            platform = row["platform"]
            new_retry = int(row["retry_count"]) + 1

            if not retryable or new_retry >= self.max_retries:
                next_status = JobLedgerStatus.DEAD_LETTER
                logger.error(
                    f"[job_id={job_id} | platform={platform}] Transactional NACK committed -> "
                    f"status=DEAD_LETTER (retry={new_retry}/{self.max_retries}, retryable={retryable}). "
                    f"Error: {clean_err}"
                )
            else:
                next_status = JobLedgerStatus.FAILED_RETRYABLE
                logger.warning(
                    f"[job_id={job_id} | platform={platform}] Transactional NACK committed -> "
                    f"status=FAILED_RETRYABLE (retry={new_retry}/{self.max_retries}). "
                    f"Error: {clean_err}"
                )

            conn.execute(
                """
                UPDATE job_ledger
                SET status = ?,
                    retry_count = ?,
                    leased_until = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE job_id = ?;
                """,
                (next_status.value, new_retry, clean_err, now_iso, job_id),
            )

        return next_status

    def reject_preflight_job(self, job_id: str, rejection_reason: str) -> bool:
        """
        Marks a job as `REJECTED_PREFLIGHT` when `preflight_gate.py` determines the page
        is a Soft-404, login wall, empty stub, or mojibake corruption.
        Prevents wasting future retries on permanently invalid pages.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_reason = (rejection_reason or "PREFLIGHT_REJECTED").strip()[:500]

        with self._get_connection(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE job_ledger
                SET status = ?,
                    leased_until = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE job_id = ?;
                """,
                (JobLedgerStatus.REJECTED_PREFLIGHT.value, clean_reason, now_iso, job_id),
            )
            updated = cursor.rowcount > 0

        if updated:
            logger.warning(
                f"[job_id={job_id}] Marked as REJECTED_PREFLIGHT (reason='{clean_reason}')."
            )
        else:
            logger.warning(f"[job_id={job_id}] reject_preflight_job called on unknown job_id.")
        return updated

    # =================================================================
    # 4. Query & Telemetry Helpers
    # =================================================================

    def get_job(self, job_id: str) -> Optional[LedgerJobRecord]:
        """Retrieves the current ledger state for a specific `job_id`."""
        with self._get_connection(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM job_ledger WHERE job_id = ?;", (job_id,)
            ).fetchone()
        return self._row_to_model(row) if row else None

    def get_ledger_stats(self) -> Dict[str, Any]:
        """
        Returns a comprehensive breakdown of job counts by status and by platform
        for pipeline monitoring and CLI reporting.
        """
        stats_by_status: Dict[str, int] = {status.value: 0 for status in JobLedgerStatus}
        stats_by_platform: Dict[str, Dict[str, int]] = {}

        with self._get_connection(immediate=False) as conn:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM job_ledger GROUP BY status;"
            ).fetchall()
            for r in status_rows:
                stats_by_status[r["status"]] = int(r["cnt"])

            platform_rows = conn.execute(
                "SELECT platform, status, COUNT(*) as cnt FROM job_ledger GROUP BY platform, status;"
            ).fetchall()
            for r in platform_rows:
                plat = r["platform"]
                if plat not in stats_by_platform:
                    stats_by_platform[plat] = {s.value: 0 for s in JobLedgerStatus}
                stats_by_platform[plat][r["status"]] = int(r["cnt"])

        total_jobs = sum(stats_by_status.values())
        summary = {
            "total_jobs": total_jobs,
            "by_status": stats_by_status,
            "by_platform": stats_by_platform,
        }
        logger.debug(f"Ledger telemetry snapshot -> total={total_jobs}, by_status={stats_by_status}")
        return summary

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> LedgerJobRecord:
        """Converts a SQLite Row object into a validated `LedgerJobRecord` Pydantic model."""
        return LedgerJobRecord(
            job_id=row["job_id"],
            url=row["url"],
            url_hash=row["url_hash"],
            platform=row["platform"],
            status=JobLedgerStatus(row["status"]),
            retry_count=int(row["retry_count"]),
            leased_until=datetime.fromisoformat(row["leased_until"]) if row["leased_until"] else None,
            last_error=row["last_error"],
            discovered_at=datetime.fromisoformat(row["discovered_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


# =====================================================================
# Standalone Verification Suite
# =====================================================================

if __name__ == "__main__":
    print("\n--- RUNNING STATE_LEDGER.PY ACID & LIFECYCLE VERIFICATION SUITE ---\n")
    test_db_path = SCRAPER_ROOT_DIR / "logs" / "test_scraper_ledger.db"
    if test_db_path.exists():
        test_db_path.unlink()

    # Initialize ledger with a fast 1-second lease timeout to test stalled lease recovery
    ledger = StateLedger(db_path=test_db_path, lease_timeout_seconds=1, max_retries=2)

    # 1. Test URL Seeding & SHA-256 Deduplication (including UTM tracking noise)
    seed_stats = ledger.register_urls(
        platform="jobinja",
        urls=[
            "https://jobinja.ir/companies/fanoos/jobs/ai-1",
            "https://jobinja.ir/companies/fanoos/jobs/ai-1?utm_source=telegram#apply",  # Duplicate after canonicalization!
            "https://jobinja.ir/companies/fanoos/jobs/backend-2",
            "https://jobinja.ir/companies/fanoos/jobs/dead-404",
        ],
    )
    print(f"✅ Test 1 (Seeding & Dedup) : {seed_stats}")

    # 2. Test Atomic Leasing & ACK (Job 1 -> COMPLETED)
    lease1 = ledger.lease_next_job(platform="jobinja")
    assert lease1 is not None
    ledger.ack_job(lease1.job_id)
    job1_after = ledger.get_job(lease1.job_id)
    print(f"✅ Test 2 (Lease & ACK)     : job_id={job1_after.job_id} | status={job1_after.status.value}")

    # 3. Test Stalled Lease Timeout Reclamation on Job 2
    lease2 = ledger.lease_next_job(platform="jobinja")
    assert lease2 is not None
    print(f"⏳ Waiting 1.2s to simulate crashed worker on {lease2.job_id}...")
    time.sleep(1.2)
    reclaimed = ledger.reclaim_expired_leases()
    print(f"✅ Test 3 (Lease Reclaim)   : Reclaimed={reclaimed} stalled job(s) -> moved to FAILED_RETRYABLE")

    # 4. Next lease pulls Job 3 first (because fresh PENDING jobs have higher priority than FAILED_RETRYABLE!)
    lease3 = ledger.lease_next_job(platform="jobinja")
    assert lease3 is not None and lease3.job_id != lease2.job_id
    ledger.reject_preflight_job(lease3.job_id, "SOFT_404_OR_ERROR_PAGE")
    job3_after = ledger.get_job(lease3.job_id)
    print(f"✅ Test 4 (Preflight Reject): job_id={job3_after.job_id} (PENDING prioritized) | status={job3_after.status.value}")

    # 5. Now that all PENDING jobs are done, leasing pulls Job 2 (FAILED_RETRYABLE) -> NACK to DEAD_LETTER
    lease2_retry = ledger.lease_next_job(platform="jobinja")
    assert lease2_retry is not None and lease2_retry.job_id == lease2.job_id
    final_status = ledger.nack_job(lease2_retry.job_id, "Cloudflare 403 block persisted", retryable=True)
    print(f"✅ Test 5 (Max Retry NACK)  : job_id={lease2_retry.job_id} | final_status={final_status.value}")

    # 6. Print Final Ledger Telemetry Snapshot
    final_stats = ledger.get_ledger_stats()
    print(f"✅ Test 6 (Ledger Telemetry): {final_stats['by_status']}\n")

    # Clean up temporary test database files
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{test_db_path}{suffix}")
        if p.exists():
            p.unlink()