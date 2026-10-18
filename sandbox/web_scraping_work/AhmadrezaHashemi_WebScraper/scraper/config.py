"""
Module: config.py
Description: Loads and validates global environment variables (.env) and external platform 
scraping configurations (sites_config.yaml). Enforces fail-fast validation upon startup 
to guarantee required network parameters, ledger paths, and proxy strings exist before execution.
"""
import logging
import os
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator, model_validator

try:
    from logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("config")


# =====================================================================
# Base Paths
# =====================================================================

SCRAPER_ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_YAML_PATH = SCRAPER_ROOT_DIR / "config" / "sites_config.yaml"
DEFAULT_ENV_PATH = SCRAPER_ROOT_DIR / ".env"


# =====================================================================
# Pydantic Schemas for sites_config.yaml
# =====================================================================

class PlatformSelectors(BaseModel):
    """Validates CSS/XPath selector definitions for a single job platform."""
    job_links: Union[str, List[str]] = Field(..., description="Selectors to extract job URLs on listing pages")
    title: Union[str, List[str]] = Field(..., description="Selectors for raw job title")
    company: Optional[Union[str, List[str]]] = Field(default=None, description="Selectors for hiring organization")
    location: Optional[Union[str, List[str]]] = Field(default=None, description="Selectors for job city/province")
    info_box: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Selectors for top metadata/specification box (salary, experience, skills)"
    )
    description: Union[str, List[str]] = Field(..., description="Selectors for main job description text")

    @field_validator("job_links", "title", "description")
    @classmethod
    def ensure_non_empty_selector(cls, v: Union[str, List[str]]) -> Union[str, List[str]]:
        if isinstance(v, str) and not v.strip():
            raise ValueError("Selector string cannot be empty.")
        if isinstance(v, list) and (not v or not any(s.strip() for s in v)):
            raise ValueError("Selector list must contain at least one non-empty selector string.")
        return v


class PlatformConfig(BaseModel):
    """Validates scraping parameters and selectors for a single Iranian job board."""
    enabled: bool = Field(default=True)
    base_url: HttpUrl
    discovery_urls: List[HttpUrl] = Field(..., min_length=1)
    requires_js: bool = Field(default=False, description="Whether Playwright JS rendering is required by default")
    rate_limit_seconds: float = Field(default=2.5, ge=0.5, le=60.0)
    max_pages_per_seed: int = Field(default=3, ge=1, le=50)
    selectors: PlatformSelectors


class SitesConfigFile(BaseModel):
    """Root schema for config/sites_config.yaml."""
    platforms: Dict[str, PlatformConfig]

    @model_validator(mode="after")
    def verify_at_least_one_enabled(self) -> "SitesConfigFile":
        if not self.platforms:
            raise ValueError("sites_config.yaml contains no platforms.")
        enabled_platforms = [k for k, v in self.platforms.items() if v.enabled]
        if not enabled_platforms:
            raise ValueError("At least one platform in sites_config.yaml must have 'enabled: true'.")
        return self


# =====================================================================
# Pydantic Schema for Global Environment (.env)
# =====================================================================

class ScraperEnvSettings(BaseModel):
    """Validates global runtime settings loaded from .env or OS environment."""
    environment: Literal["development", "staging", "production"] = Field(default="development")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    log_dir: Path = Field(default=SCRAPER_ROOT_DIR / "logs")
    ledger_db_path: Path = Field(default=SCRAPER_ROOT_DIR / "scraper_ledger.db")

    # Network & Transport Parameters
    http_timeout_seconds: float = Field(default=20.0, ge=5.0, le=120.0)
    max_retries: int = Field(default=3, ge=1, le=10)
    chrome_impersonate_version: str = Field(default="chrome124")
    playwright_headless: bool = Field(default=True)

    # Proxy Routing (Supports splitting domestic .ir traffic from external traffic)
    proxy_url: Optional[str] = Field(default=None)
    bypass_proxy_for_ir_domains: bool = Field(
        default=True,
        description="When True, domestic Iranian job boards are fetched directly without a foreign proxy to avoid ArvanCloud blocks"
    )

    # Content Gate & State Ledger Thresholds
    min_description_chars: int = Field(default=80, ge=20)
    lease_timeout_seconds: int = Field(default=300, ge=30)

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_scheme(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        valid_prefixes = ("http://", "https://", "socks5://", "socks5h://")
        if not v.startswith(valid_prefixes):
            raise ValueError(f"PROXY_URL must start with one of {valid_prefixes}. Got: '{v}'")
        return v


# =====================================================================
# Unified Configuration Loader (Fail-Fast Singleton)
# =====================================================================

class AppConfig:
    """
    Central configuration object holding validated `env` and `sites` configurations.
    """

    def __init__(self, env_path: Optional[Path] = None, yaml_path: Optional[Path] = None):
        self.env_path = env_path or DEFAULT_ENV_PATH
        self.yaml_path = yaml_path or DEFAULT_YAML_PATH

        logger.info(f"Initializing AppConfig (env_path='{self.env_path}', yaml_path='{self.yaml_path}')...")
        self.env: ScraperEnvSettings = self._load_and_validate_env()
        self.sites: SitesConfigFile = self._load_and_validate_yaml()
        self._ensure_directories()
        logger.info(
            f"AppConfig loaded successfully. Active platforms: "
            f"{list(self.get_enabled_platforms().keys())}"
        )

    def _load_and_validate_env(self) -> ScraperEnvSettings:
        if self.env_path.exists():
            load_dotenv(dotenv_path=self.env_path, override=False)
            logger.debug(f"Loaded environment variables from '{self.env_path}'.")
        else:
            logger.warning(
                f"No .env file found at '{self.env_path}'. Using OS environment variables and safe defaults."
            )

        raw_env_data = {
            "environment": os.getenv("SCRAPER_ENV", "development"),
            "log_level": os.getenv("SCRAPER_LOG_LEVEL", "INFO").upper(),
            "log_dir": Path(os.getenv("SCRAPER_LOG_DIR", str(SCRAPER_ROOT_DIR / "logs"))),
            "ledger_db_path": Path(os.getenv("SCRAPER_LEDGER_DB_PATH", str(SCRAPER_ROOT_DIR / "scraper_ledger.db"))),
            "http_timeout_seconds": float(os.getenv("SCRAPER_HTTP_TIMEOUT", "20.0")),
            "max_retries": int(os.getenv("SCRAPER_MAX_RETRIES", "3")),
            "chrome_impersonate_version": os.getenv("SCRAPER_IMPERSONATE", "chrome124"),
            "playwright_headless": os.getenv("SCRAPER_PLAYWRIGHT_HEADLESS", "true").lower() in ("1", "true", "yes"),
            "proxy_url": os.getenv("SCRAPER_PROXY_URL", None),
            "bypass_proxy_for_ir_domains": os.getenv("SCRAPER_BYPASS_IR_PROXY", "true").lower() in ("1", "true", "yes"),
            "min_description_chars": int(os.getenv("SCRAPER_MIN_DESC_CHARS", "80")),
            "lease_timeout_seconds": int(os.getenv("SCRAPER_LEASE_TIMEOUT", "300")),
        }

        try:
            settings = ScraperEnvSettings.model_validate(raw_env_data)
            logger.debug(
                f"Validated ScraperEnvSettings -> env={settings.environment}, "
                f"timeout={settings.http_timeout_seconds}s, retries={settings.max_retries}, "
                f"bypass_ir_proxy={settings.bypass_proxy_for_ir_domains}"
            )
            return settings
        except ValidationError as e:
            logger.critical(f"FAIL-FAST: Invalid environment configuration in .env:\n{e}")
            raise SystemExit(f"Configuration Error (.env): {e}") from e

    def _load_and_validate_yaml(self) -> SitesConfigFile:
        if not self.yaml_path.exists():
            logger.critical(f"FAIL-FAST: Required platform config file not found at '{self.yaml_path}'.")
            raise SystemExit(f"Missing required configuration file: {self.yaml_path}")

        try:
            with open(self.yaml_path, "r", encoding="utf-8") as f:
                raw_yaml = yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.critical(f"FAIL-FAST: Malformed YAML syntax in '{self.yaml_path}': {e}")
            raise SystemExit(f"YAML Syntax Error in {self.yaml_path}: {e}") from e

        if not isinstance(raw_yaml, dict):
            logger.critical(f"FAIL-FAST: '{self.yaml_path}' root must be a YAML mapping/dictionary.")
            raise SystemExit(f"Invalid YAML root structure in {self.yaml_path}")

        try:
            sites_cfg = SitesConfigFile.model_validate(raw_yaml)
            for name, p_cfg in sites_cfg.platforms.items():
                logger.debug(
                    f"Validated platform '{name}' -> enabled={p_cfg.enabled}, "
                    f"requires_js={p_cfg.requires_js}, rate_limit={p_cfg.rate_limit_seconds}s, "
                    f"discovery_urls={len(p_cfg.discovery_urls)}"
                )
            return sites_cfg
        except ValidationError as e:
            logger.critical(f"FAIL-FAST: Schema validation failed for '{self.yaml_path}':\n{e}")
            raise SystemExit(f"Configuration Error (sites_config.yaml): {e}") from e

    def _ensure_directories(self) -> None:
        """Guarantees log directory and SQLite ledger parent directory exist before pipeline runs."""
        self.env.log_dir.mkdir(parents=True, exist_ok=True)
        self.env.ledger_db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(
            f"Verified filesystem paths -> log_dir='{self.env.log_dir}', "
            f"ledger_db='{self.env.ledger_db_path}'"
        )

    def get_enabled_platforms(self) -> Dict[str, PlatformConfig]:
        """Returns only the platforms marked with enabled: true."""
        return {name: cfg for name, cfg in self.sites.platforms.items() if cfg.enabled}

    def get_platform(self, platform_name: str) -> PlatformConfig:
        """Retrieves configuration for a specific platform or raises KeyError."""
        key = platform_name.lower().strip()
        if key not in self.sites.platforms:
            raise KeyError(f"Platform '{platform_name}' is not defined in {self.yaml_path}")
        return self.sites.platforms[key]


# Helper factory with lazy caching
_CONFIG_INSTANCE: Optional[AppConfig] = None


def get_config(force_reload: bool = False) -> AppConfig:
    """Returns the singleton AppConfig instance."""
    global _CONFIG_INSTANCE
    if _CONFIG_INSTANCE is None or force_reload:
        _CONFIG_INSTANCE = AppConfig()
    return _CONFIG_INSTANCE


if __name__ == "__main__":
    print("\n--- RUNNING CONFIG.PY FAIL-FAST VALIDATION TEST ---\n")
    cfg = get_config()
    print(f"✅ Environment     : {cfg.env.environment}")
    print(f"✅ Ledger DB Path  : {cfg.env.ledger_db_path}")
    print(f"✅ Log Directory   : {cfg.env.log_dir}")
    print(f"✅ Bypass .ir Proxy: {cfg.env.bypass_proxy_for_ir_domains}")
    print(f"✅ Enabled Sites   : {list(cfg.get_enabled_platforms().keys())}")
    for p_name, p_data in cfg.get_enabled_platforms().items():
        print(
            f"   -> {p_name:14s} | base={p_data.base_url} | "
            f"js={str(p_data.requires_js):5s} | rate_limit={p_data.rate_limit_seconds}s"
        )