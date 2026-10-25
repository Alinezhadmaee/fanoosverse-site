"""Tests for the startup check and config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from normalizer_agent.schemas.job_ad import JobAd
from normalizer_agent.settings import (
    ConfigError,
    FieldRule,
    FieldsConfig,
    LLMConfig,
    PipelineConfig,
    ProviderConfig,
    check_provider_env,
    load_app_config,
    load_llm_config,
)
from normalizer_agent.startup_check import FieldsConfigMismatchError, check_fields_config

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def test_shipped_fields_yaml_matches_schema() -> None:
    fields = FieldsConfig.model_validate(_load_yaml(CONFIG_DIR / "fields.yaml"))
    check_fields_config(fields, JobAd)  # must not raise


def _load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _fields(fields: dict) -> FieldsConfig:
    return FieldsConfig.model_validate({"fields": fields})


def test_missing_field_detected() -> None:
    fields = _fields({"role_title": {"kind": "passthrough"}})
    with pytest.raises(FieldsConfigMismatchError) as excinfo:
        check_fields_config(fields, JobAd)
    messages = " ".join(excinfo.value.problems)
    assert "document_id" in messages and "missing from fields.yaml" in messages


def test_extra_field_detected() -> None:
    fields = _fields({**_ship_all(), "nonexistent_field": {"kind": "passthrough"}})
    with pytest.raises(FieldsConfigMismatchError) as excinfo:
        check_fields_config(fields, JobAd)
    assert "not in the schema" in " ".join(excinfo.value.problems)


def _ship_all() -> dict:
    import yaml

    data = yaml.safe_load((CONFIG_DIR / "fields.yaml").read_text(encoding="utf-8"))
    return data["fields"]


def test_incompatible_kind_detected() -> None:
    fields = _fields({**_ship_all(), "skills_required": {"kind": "integer"}})
    with pytest.raises(FieldsConfigMismatchError) as excinfo:
        check_fields_config(fields, JobAd)
    messages = " ".join(excinfo.value.problems)
    assert "skills_required" in messages and "list" in messages


def test_enum_kind_enforced() -> None:
    fields = _fields({**_ship_all(), "work_mode": {"kind": "vocab_list", "vocab": "work_mode"}})
    with pytest.raises(FieldsConfigMismatchError) as excinfo:
        check_fields_config(fields, JobAd)
    assert "enum list" in " ".join(excinfo.value.problems)


def test_shipped_configs_load() -> None:
    app = load_app_config(CONFIG_DIR)
    assert app.fields.rule("skills_required").kind == "vocab_list"
    assert app.llm.routing.strategy == "priority_fallback"
    assert len(app.llm.providers) >= 1


def test_llm_config_requires_enabled_provider(tmp_path: Path) -> None:
    path = tmp_path / "llm.yaml"
    path.write_text(
        "routing: {strategy: priority_fallback}\n"
        "providers:\n"
        "  - {name: a, type: mock, model: m, enabled: false, priority: 1}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="no enabled provider"):
        load_llm_config(path)


def test_llm_config_duplicate_names_rejected(tmp_path: Path) -> None:
    path = tmp_path / "llm.yaml"
    path.write_text(
        "providers:\n"
        "  - {name: a, type: mock, model: m, priority: 1}\n"
        "  - {name: a, type: mock, model: m2, priority: 2}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate provider names"):
        load_llm_config(path)


def test_missing_env_var_is_warning() -> None:
    cfg = LLMConfig(
        providers=[ProviderConfig(name="p", type="mock", model="m", api_key_env="MISSING_KEY_X")]
    )
    warnings = check_provider_env(cfg)
    assert any("MISSING_KEY_X" in w for w in warnings)


def test_pipeline_defaults() -> None:
    cfg = PipelineConfig()
    assert cfg.llm_mode == "candidates"
    assert cfg.placeholder_policy == "keep_enum_member"
    assert "CI/CD" in cfg.composite_whitelist


def test_field_rule_vocab_required_for_vocab_kinds() -> None:
    with pytest.raises(ValueError, match="vocab"):
        FieldRule(kind="vocab_list")
