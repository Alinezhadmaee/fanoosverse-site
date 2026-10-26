"""Optional HTTP REST microservice wrapper around the Normalizer Agent.

Endpoints:
  POST /normalize         - Normalize a single job advertisement
  POST /normalize/batch   - Normalize a list of job advertisements
  GET  /health            - Health and liveness probe
  GET  /metrics           - Pipeline metrics counters
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from normalizer_agent import __version__
from normalizer_agent.interface.api import Normalizer
from normalizer_agent.schemas.envelope import NormalizeResult


class NormalizeRequest(BaseModel):
    record: dict[str, Any]
    record_id: str | None = None
    trace_id: str | None = None


class BatchNormalizeRequest(BaseModel):
    records: list[dict[str, Any]]
    trace_ids: list[str | None] | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    providers_enabled: list[str]


_normalizer: Normalizer | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _normalizer
    config_dir = Path(os.getenv("NORMALIZER_CONFIG_DIR", "config"))
    _normalizer = await Normalizer.from_config(config_dir)
    yield
    if _normalizer is not None:
        await _normalizer.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fanos Normalizer Agent API",
        version=__version__,
        description="REST service for Iranian job-ad deterministic-first normalization",
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        if _normalizer is None:
            raise HTTPException(status_code=503, detail="Normalizer uninitialized")
        return HealthResponse(
            status="healthy",
            version=__version__,
            providers_enabled=[p.name for p in _normalizer.config.llm.enabled_providers()],
        )

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        if _normalizer is None:
            raise HTTPException(status_code=503, detail="Normalizer uninitialized")
        return _normalizer.metrics.as_dict()

    @app.post("/normalize", response_model=NormalizeResult)
    async def normalize_single(req: NormalizeRequest) -> NormalizeResult:
        if _normalizer is None:
            raise HTTPException(status_code=503, detail="Normalizer uninitialized")
        return await _normalizer.normalize(
            req.record,
            record_id=req.record_id,
            trace_id=req.trace_id,
        )

    @app.post("/normalize/batch", response_model=list[NormalizeResult])
    async def normalize_batch(req: BatchNormalizeRequest) -> list[NormalizeResult]:
        if _normalizer is None:
            raise HTTPException(status_code=503, detail="Normalizer uninitialized")
        return await _normalizer.normalize_batch(
            req.records,
            trace_ids=req.trace_ids,
        )

    return app


app = create_app()
