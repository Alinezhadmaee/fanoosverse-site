"""Tests for Phase 6 interfaces: CLI, Coordinator adapter, and REST service."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from normalizer_agent.interface.cli import _build_parser, _validate_config_command
from normalizer_agent.interface.coordinator import (
    ReferenceCoordinatorAdapter,
)
from normalizer_agent.interface.service import create_app
from normalizer_agent.llm.providers.mock import MockProvider
from normalizer_agent.llm.router import FallbackRouter, _Entry
from normalizer_agent.settings import LLMConfig, ProviderConfig, RoutingConfig


def _create_router_with_provider(
    provider, app_config, name="mock_test", priority=1
) -> FallbackRouter:
    p_cfg = ProviderConfig(name=name, type="mock", model="mock-model", priority=priority)
    llm_cfg = LLMConfig(
        routing=RoutingConfig(strategy="priority_fallback", max_total_attempts=4),
        providers=[p_cfg],
    )
    router = FallbackRouter.__new__(FallbackRouter)
    router._llm_config = llm_cfg
    router._pipeline = app_config.pipeline
    entry = _Entry(p_cfg, app_config.pipeline, provider=provider)
    router._entries = [entry]
    router._rr_index = 0
    router._call_lock = asyncio.Lock()
    return router


@dataclass
class SimpleMsg:
    id: str
    body: dict[str, Any]
    trace_id: str | None = None


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_raw(project_root: Path) -> dict[str, Any]:
    path = project_root / "tests" / "data" / "sample_record_1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_cli_parser_and_validation(project_root: Path):
    parser = _build_parser()
    args = parser.parse_args(["validate-config", "-c", str(project_root / "config")])
    assert args.command == "validate-config"
    res = _validate_config_command(args)
    assert res == 0


@pytest.mark.asyncio
async def test_coordinator_adapter(project_root: Path, sample_raw):
    from normalizer_agent.interface.api import Normalizer

    normalizer = await Normalizer.from_config(project_root / "config")
    p_cfg = ProviderConfig(name="mock_test", type="mock", model="mock-model", priority=1)
    provider = MockProvider(p_cfg)
    mock_router = _create_router_with_provider(provider, normalizer.config)
    normalizer.router = mock_router
    normalizer._normalizer.router = mock_router
    normalizer._normalizer.llm_stage.router = mock_router

    adapter = ReferenceCoordinatorAdapter(normalizer)
    msg1 = SimpleMsg(id="msg_001", body=sample_raw, trace_id="tr_01")

    res = await adapter.process_item(msg1)
    assert res.record_id == "msg_001"
    assert len(adapter.published_canonical) + len(adapter.published_review) == 1

    msg2 = SimpleMsg(id="msg_002", body={"bad": "record"}, trace_id="tr_02")
    await adapter.process_item(msg2)
    assert len(adapter.published_dead_letter) == 1
    await normalizer.aclose()


def test_fastapi_service_endpoints(project_root: Path, sample_raw, monkeypatch):
    monkeypatch.setenv("NORMALIZER_CONFIG_DIR", str(project_root / "config"))
    app = create_app()
    with TestClient(app) as client:
        # Patch router inside app lifespan to mock
        import normalizer_agent.interface.service as srv

        if srv._normalizer is not None:
            p_cfg = ProviderConfig(name="mock_test", type="mock", model="mock-model", priority=1)
            provider = MockProvider(p_cfg)
            mock_router = _create_router_with_provider(provider, srv._normalizer.config)
            srv._normalizer.router = mock_router
            srv._normalizer._normalizer.router = mock_router
            srv._normalizer._normalizer.llm_stage.router = mock_router

        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

        m_resp = client.get("/metrics")
        assert m_resp.status_code == 200
        assert "records_in" in m_resp.json()

        post_resp = client.post(
            "/normalize", json={"record": sample_raw, "record_id": "api_test_01"}
        )
        assert post_resp.status_code == 200
        data = post_resp.json()
        assert data["record_id"] == "api_test_01"
        assert data["status"] in ("normalized", "needs_review")
