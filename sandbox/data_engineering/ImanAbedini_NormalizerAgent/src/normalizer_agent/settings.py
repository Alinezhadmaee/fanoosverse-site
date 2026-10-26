"""Configuration loading and validation (llm.yaml, fields.yaml, pipeline.yaml).

All file paths are resolved from a config directory (default: ``config/`` next to
the project root). ``NORMALIZER_LLM_CONFIG`` may point at an alternative llm.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Kind = Literal[
    "passthrough",
    "vocab_list",
    "vocab_single",
    "enum_list",
    "enum_single",
    "integer",
    "integer_range_min",
    "integer_range_max",
    "money",
    "money_currency",
    "date",
    "location",
    "free_text_list",
]


class ConfigError(Exception):
    """Raised when a configuration file is missing, malformed, or inconsistent."""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"top-level YAML in {path} must be a mapping, got {type(data).__name__}")
    return data


# --------------------------------------------------------------------------- #
# fields.yaml
# --------------------------------------------------------------------------- #


class FieldRule(BaseModel):
    """Normalization behaviour of one schema field.

    Unknown extra keys are allowed so new options can be introduced without a
    code change, but the ones below are typed and validated.
    """

    model_config = ConfigDict(extra="allow")

    kind: Kind
    vocab: str | None = None
    allow_composite_split: bool = False
    is_skill: bool = False
    category: str | None = None
    explicit_no_member: str | None = None
    pair: str | None = None
    currency_field: str | None = None
    unit: str | None = None
    value_range: tuple[int, int] | None = Field(default=None, alias="range")

    @model_validator(mode="after")
    def _vocab_required(self) -> FieldRule:
        vocab_kinds = {"vocab_list", "vocab_single", "enum_list", "enum_single", "location"}
        if self.kind in vocab_kinds and not self.vocab:
            raise ValueError(f"kind '{self.kind}' requires a 'vocab' entry")
        return self


class FieldsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, FieldRule]

    def rule(self, field_name: str) -> FieldRule | None:
        return self.fields.get(field_name)

    def fields_of_kind(self, *kinds: str) -> list[str]:
        return [name for name, rule in self.fields.items() if rule.kind in kinds]


# --------------------------------------------------------------------------- #
# llm.yaml
# --------------------------------------------------------------------------- #


class ProviderFeatures(BaseModel):
    model_config = ConfigDict(extra="allow")

    json_mode: bool = False
    prompt_caching: bool = False


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    type: str  # validated against the provider registry at build time
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    priority: int = 1
    max_retries: int = 2
    timeout_s: float = 60.0
    params: dict[str, Any] = Field(default_factory=dict)
    features: ProviderFeatures = Field(default_factory=ProviderFeatures)
    cost_per_mtok: dict[str, float] = Field(default_factory=dict)
    requests_per_minute: float | None = None

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        in_cost = self.cost_per_mtok.get("input", 0.0) * input_tokens / 1_000_000
        out_cost = self.cost_per_mtok.get("output", 0.0) * output_tokens / 1_000_000
        return in_cost + out_cost


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["priority_fallback", "round_robin"] = "priority_fallback"
    max_total_attempts: int = 6
    request_timeout_s: float = 60.0


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    providers: list[ProviderConfig]

    @field_validator("providers")
    @classmethod
    def _check_providers(cls, value: list[ProviderConfig]) -> list[ProviderConfig]:
        names = [p.name for p in value]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate provider names: {dupes}")
        if not any(p.enabled for p in value):
            raise ValueError("no enabled provider in llm.yaml")
        return value

    def enabled_providers(self) -> list[ProviderConfig]:
        return sorted((p for p in self.providers if p.enabled), key=lambda p: (p.priority, p.name))


def check_provider_env(config: LLMConfig) -> list[str]:
    """Return human-readable warnings for enabled providers whose API key is absent.

    A missing key for any provider is only a warning (a backup may be unusable),
    but the caller must crash if no usable provider remains.
    """

    warnings: list[str] = []
    for provider in config.enabled_providers():
        if provider.api_key_env and provider.api_key_env not in os.environ:
            warnings.append(
                f"provider '{provider.name}': env var {provider.api_key_env} is not set"
            )
    return warnings


# --------------------------------------------------------------------------- #
# pipeline.yaml
# --------------------------------------------------------------------------- #


class CacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    path: str = ".normalizer_cache.sqlite3"
    key_includes_model: bool = False


class CircuitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = 3
    cooldown_s: float = 60.0
    permanent_cooldown_s: float = 3600.0


class ConcurrencyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = 4


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0


class NumericSanityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    salary_min_irr_warn: int = 1_000_000
    salary_max_irr_warn: int = 10_000_000_000
    age_range: tuple[int, int] = (14, 80)
    experience_range: tuple[int, int] = (0, 50)


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_mode: Literal["direct", "candidates", "full_list"] = "candidates"
    candidates_top_k: int = 8
    full_list_max_values: int = 400
    min_accept_confidence: Literal["high", "low"] = "high"
    placeholder_policy: Literal["drop", "keep_enum_member"] = "keep_enum_member"
    record_raw_in_evidence_notes: bool = True
    composite_whitelist: list[str] = Field(
        default_factory=lambda: [
            "CI/CD",
            "A/B Testing",
            "ML/AI",
            "R&D",
            "PL/SQL",
            "Node.js",
            "Next.js",
            "Vue.js",
            "Power BI",
        ]
    )
    llm_max_tokens: int = 2048
    log_level: str = "INFO"
    cache: CacheConfig = Field(default_factory=CacheConfig)
    circuit_breaker: CircuitConfig = Field(default_factory=CircuitConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    retries: RetryConfig = Field(default_factory=RetryConfig)
    numeric: NumericSanityConfig = Field(default_factory=NumericSanityConfig)


# --------------------------------------------------------------------------- #
# Bundle
# --------------------------------------------------------------------------- #


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config_dir: Path
    fields: FieldsConfig
    llm: LLMConfig
    pipeline: PipelineConfig
    warnings: list[str] = Field(default_factory=list)


def load_fields_config(path: Path) -> FieldsConfig:
    data = _load_yaml(path)
    try:
        return FieldsConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"invalid fields.yaml ({path}): {exc}") from exc


def load_llm_config(path: Path | None = None) -> LLMConfig:
    env_path = os.environ.get("NORMALIZER_LLM_CONFIG")
    if env_path and path is None:
        path = Path(env_path)
    if path is None:
        raise ConfigError("no llm.yaml path given and NORMALIZER_LLM_CONFIG is not set")
    data = _load_yaml(path)
    try:
        return LLMConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"invalid llm.yaml ({path}): {exc}") from exc


def load_pipeline_config(path: Path) -> PipelineConfig:
    data = _load_yaml(path)
    try:
        return PipelineConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"invalid pipeline.yaml ({path}): {exc}") from exc


def load_app_config(config_dir: Path | str) -> AppConfig:
    config_dir = Path(config_dir)
    fields = load_fields_config(config_dir / "fields.yaml")
    llm = load_llm_config(config_dir / "llm.yaml")
    pipeline = load_pipeline_config(config_dir / "pipeline.yaml")
    return AppConfig(
        config_dir=config_dir,
        fields=fields,
        llm=llm,
        pipeline=pipeline,
        warnings=check_provider_env(llm),
    )
