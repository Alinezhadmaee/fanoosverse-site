"""
Module: logger.py
Description: Configures structured, production-grade logging for the scraping pipeline.
Provides dual-sink output (console with human-readable formatting and daily rotating files 
in JSON format). Automatically binds contextual execution trace IDs (job_id, platform, 
request_duration) to track anomalies and debug scraping failures across external sites.
"""
import contextvars
import json
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from dotenv import load_dotenv

# =====================================================================
# Environment & Path Resolution (Circular-Import Safe)
# =====================================================================

SCRAPER_ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE_PATH = SCRAPER_ROOT_DIR / ".env"

if ENV_FILE_PATH.exists():
    load_dotenv(dotenv_path=ENV_FILE_PATH, override=False)

DEFAULT_LOG_LEVEL = os.getenv("SCRAPER_LOG_LEVEL", "DEBUG").upper()
DEFAULT_LOG_DIR = Path(os.getenv("SCRAPER_LOG_DIR", str(SCRAPER_ROOT_DIR / "logs")))
DEFAULT_JSON_LOG_FILENAME = "scraper.json.log"

# Ensure UTF-8 console output on Windows/Linux terminals for Persian characters
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


# =====================================================================
# Contextual Trace State (Thread & Async Safe via contextvars)
# =====================================================================

_TRACE_CONTEXT: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "fanoos_scraper_trace_context", default={}
)


def bind_log_context(
    job_id: Optional[str] = None,
    platform: Optional[str] = None,
    url: Optional[str] = None,
    request_duration_ms: Optional[float] = None,
    extraction_tier: Optional[str] = None,
    trace_id: Optional[str] = None,
    **extra_fields: Any,
) -> contextvars.Token:
    """
    Binds contextual execution metadata to the current execution flow.
    Returns a token that can be used to restore the previous context state.
    """
    current = dict(_TRACE_CONTEXT.get())
    updates = {
        "trace_id": trace_id or current.get("trace_id") or uuid.uuid4().hex[:8],
        "job_id": job_id if job_id is not None else current.get("job_id", "N/A"),
        "platform": platform if platform is not None else current.get("platform", "system"),
        "url": url if url is not None else current.get("url"),
        "request_duration_ms": (
            round(request_duration_ms, 2)
            if request_duration_ms is not None
            else current.get("request_duration_ms")
        ),
        "extraction_tier": (
            extraction_tier if extraction_tier is not None else current.get("extraction_tier")
        ),
    }
    updates.update(extra_fields)
    # Remove None values to keep JSON payloads clean
    cleaned = {k: v for k, v in updates.items() if v is not None}
    return _TRACE_CONTEXT.set(cleaned)


def clear_log_context() -> None:
    """Resets the contextual logging state to empty."""
    _TRACE_CONTEXT.set({})


def get_current_log_context() -> Dict[str, Any]:
    """Returns a copy of the active contextual trace dictionary."""
    return dict(_TRACE_CONTEXT.get())


@contextmanager
def log_context(
    job_id: Optional[str] = None,
    platform: Optional[str] = None,
    url: Optional[str] = None,
    request_duration_ms: Optional[float] = None,
    extraction_tier: Optional[str] = None,
    trace_id: Optional[str] = None,
    **extra_fields: Any,
) -> Generator[Dict[str, Any], None, None]:
    """
    Context manager that automatically binds trace metadata for the duration of a `with` block
    and restores the prior context upon exit.
    """
    token = bind_log_context(
        job_id=job_id,
        platform=platform,
        url=url,
        request_duration_ms=request_duration_ms,
        extraction_tier=extraction_tier,
        trace_id=trace_id,
        **extra_fields,
    )
    try:
        yield get_current_log_context()
    finally:
        _TRACE_CONTEXT.reset(token)


# =====================================================================
# Context Injection Filter
# =====================================================================

class ContextTraceFilter(logging.Filter):
    """
    Injects active `contextvars` trace attributes and any `extra={...}` kwargs
    directly onto every LogRecord before formatting.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _TRACE_CONTEXT.get()
        record.trace_id = getattr(record, "trace_id", None) or ctx.get("trace_id", "-")
        record.job_id = getattr(record, "job_id", None) or ctx.get("job_id", "N/A")
        record.platform = getattr(record, "platform", None) or ctx.get("platform", "system")
        record.url = getattr(record, "url", None) or ctx.get("url", None)
        record.request_duration_ms = (
            getattr(record, "request_duration_ms", None) or ctx.get("request_duration_ms", None)
        )
        record.extraction_tier = (
            getattr(record, "extraction_tier", None) or ctx.get("extraction_tier", None)
        )
        record.extra_context = {
            k: v
            for k, v in ctx.items()
            if k not in {"trace_id", "job_id", "platform", "url", "request_duration_ms", "extraction_tier"}
        }
        return True


# =====================================================================
# Sink 1: Human-Readable Console Formatter
# =====================================================================

class ConsoleReadableFormatter(logging.Formatter):
    """
    Formats logs for terminal output with optional ANSI colors and smart context tags.
    Avoids duplicating `[job_id=...]` if the caller already embedded it in the message string.
    """

    LEVEL_COLORS = {
        logging.DEBUG: "\033[36m",     # Cyan
        logging.INFO: "\033[32m",      # Green
        logging.WARNING: "\033[33m",   # Yellow
        logging.ERROR: "\033[31m",     # Red
        logging.CRITICAL: "\033[1;41m" # Bold Red Background
    }
    RESET_COLOR = "\033[0m"

    def __init__(self, use_colors: bool = True):
        super().__init__()
        self.use_colors = use_colors and sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]

        level_name = f"{record.levelname:<8}"
        if self.use_colors and record.levelno in self.LEVEL_COLORS:
            level_name = f"{self.LEVEL_COLORS[record.levelno]}{level_name}{self.RESET_COLOR}"

        module_loc = f"{record.name}:{record.lineno}"
        message = record.getMessage()

        # Build context tag only if context is active and not already manually prepended in message
        ctx_parts = []
        job_id = getattr(record, "job_id", "N/A")
        platform = getattr(record, "platform", "system")
        duration = getattr(record, "request_duration_ms", None)

        if "[job_id=" not in message and (job_id != "N/A" or platform != "system"):
            ctx_parts.append(f"job_id={job_id}")
            ctx_parts.append(f"platform={platform}")
            if duration is not None:
                ctx_parts.append(f"duration={duration}ms")

        ctx_prefix = f"[{' | '.join(ctx_parts)}] " if ctx_parts else ""
        formatted_line = f"{timestamp} | {level_name} | {module_loc:<26} | {ctx_prefix}{message}"

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            formatted_line = f"{formatted_line}\n{exc_text}"

        return formatted_line


# =====================================================================
# Sink 2: Structured JSONL Rotating File Formatter
# =====================================================================

class StructuredJSONFormatter(logging.Formatter):
    """
    Serializes every LogRecord into a single-line UTF-8 JSON object for machine ingestion,
    anomaly detection, and production debugging. Preserves Persian characters natively.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "trace_id": getattr(record, "trace_id", "-"),
            "job_id": getattr(record, "job_id", "N/A"),
            "platform": getattr(record, "platform", "system"),
            "url": getattr(record, "url", None),
            "request_duration_ms": getattr(record, "request_duration_ms", None),
            "extraction_tier": getattr(record, "extraction_tier", None),
            "message": record.getMessage(),
        }

        extra_ctx = getattr(record, "extra_context", None)
        if extra_ctx:
            log_payload["context"] = extra_ctx

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)

        # Strip None values for compact JSONL storage while keeping core identifiers
        compact_payload = {
            k: v
            for k, v in log_payload.items()
            if v is not None or k in ("job_id", "platform", "message")
        }

        return json.dumps(compact_payload, ensure_ascii=False)


# =====================================================================
# Central Logger Factory & Configuration Manager
# =====================================================================

_LOGGING_INITIALIZED = False
_ACTIVE_LOG_DIR: Path = DEFAULT_LOG_DIR
_ACTIVE_LOG_LEVEL: int = getattr(logging, DEFAULT_LOG_LEVEL, logging.INFO)


def configure_logging(
    log_level: Optional[str] = None,
    log_dir: Optional[Path] = None,
    json_filename: str = DEFAULT_JSON_LOG_FILENAME,
    backup_days: int = 30,
) -> None:
    """
    Initializes or reconfigures the root Fanoos scraper logging handlers.
    Idempotent unless explicitly called with new parameters.
    """
    global _LOGGING_INITIALIZED, _ACTIVE_LOG_DIR, _ACTIVE_LOG_LEVEL

    resolved_level_str = (log_level or DEFAULT_LOG_LEVEL).upper()
    _ACTIVE_LOG_LEVEL = getattr(logging, resolved_level_str, logging.INFO)
    _ACTIVE_LOG_DIR = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    _ACTIVE_LOG_DIR.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger("fanoos_scraper")
    root_logger.setLevel(_ACTIVE_LOG_LEVEL)
    root_logger.propagate = False

    # Clear existing handlers on reconfiguration to prevent duplicate log lines
    if root_logger.handlers:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()

    trace_filter = ContextTraceFilter()

    # 1. Console Handler (Human-Readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(_ACTIVE_LOG_LEVEL)
    console_handler.addFilter(trace_filter)
    console_handler.setFormatter(ConsoleReadableFormatter(use_colors=True))
    root_logger.addHandler(console_handler)

    # 2. Daily Rotating JSON File Handler (Machine-Readable UTF-8)
    json_log_path = _ACTIVE_LOG_DIR / json_filename
    file_handler = TimedRotatingFileHandler(
        filename=str(json_log_path),
        when="midnight",
        interval=1,
        backupCount=backup_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(logging.DEBUG)  # Always capture full DEBUG detail in JSON files
    file_handler.addFilter(trace_filter)
    file_handler.setFormatter(StructuredJSONFormatter())
    root_logger.addHandler(file_handler)

    # Silence overly noisy 3rd-party libraries
    for noisy_lib in ("urllib3", "asyncio", "playwright", "trafilatura", "httpx", "httpcore"):
        logging.getLogger(noisy_lib).setLevel(logging.WARNING)

    _LOGGING_INITIALIZED = True
    root_logger.debug(
        f"Logging initialized (console_level={resolved_level_str}, "
        f"json_file='{json_log_path}', retention={backup_days}d)."
    )


def get_logger(name: str) -> logging.Logger:
    """
    Returns a child logger under the `fanoos_scraper` namespace so all modules
    share the dual-sink console + rotating JSON file handlers.
    """
    global _LOGGING_INITIALIZED
    if not _LOGGING_INITIALIZED:
        configure_logging()

    clean_name = name.replace("fanoos_scraper.", "")
    if clean_name == "__main__":
        clean_name = "main"
    return logging.getLogger(f"fanoos_scraper.{clean_name}")


# =====================================================================
# Standalone Verification Suite
# =====================================================================

if __name__ == "__main__":
    print("\n--- RUNNING LOGGER.PY DUAL-SINK & CONTEXT TRACE TEST ---\n")
    configure_logging(log_level="DEBUG")
    test_logger = get_logger("logger_test")

    # 1. Standard system-level log
    test_logger.info("Scraper pipeline startup check initialized.")

    # 2. Context-bound execution trace (simulating leasing a Persian job ad)
    with log_context(
        job_id="JOB-98231",
        platform="jobinja",
        url="https://jobinja.ir/companies/fanoos/jobs/AI-1",
        request_duration_ms=342.18,
        extraction_tier="TIER_2_SELECTORS",
    ):
        test_logger.info("واکنش موفق شبکه و استخراج متن آگهی استخدام مهندس هوش مصنوعی.")
        test_logger.warning("تگ حقوق در بخش خلاصه یافت نشد؛ بررسی در متن اصلی ادامه دارد.")

    # 3. Simulated exception trace
    try:
        with log_context(job_id="JOB-99999", platform="jobvision", url="https://jobvision.ir/jobs/fail"):
            raise TimeoutError("Simulated Cloudflare TLS handshake timeout after 20.0s")
    except TimeoutError:
        test_logger.exception("Network fetch failed during job lease execution.")

    json_file = DEFAULT_LOG_DIR / DEFAULT_JSON_LOG_FILENAME
    print(f"\n✅ JSON Log File Created at: {json_file}")
    if json_file.exists():
        lines = json_file.read_text(encoding="utf-8").strip().splitlines()
        print(f"✅ Total JSON Log Entries Written: {len(lines)}")
        print(f"✅ Latest JSON Log Entry Sample:\n{lines[-1]}\n")


