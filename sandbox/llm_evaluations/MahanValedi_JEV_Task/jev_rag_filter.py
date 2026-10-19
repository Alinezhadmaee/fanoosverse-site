"""Smart Gatekeeper for the RAG pipeline (Project Fanos).

This module sits between the vector database and the text-generation LLM and
decides whether the retrieved text really answers the user's question.

Flow:
    1. Query VDB: embed the user prompt and fetch the top-k chunks from Qdrant.
    2. JEV filter: for every chunk, ask a System One decision model one yes/no
       ("noul") question: "does this passage contain the answer?". The model
       is either the TypeSafe Jev API (closed) or the open-source OpenDecider
       model running locally.
    3. Threshold logic: a chunk passes only if the returned probability is
       strictly greater than GATE_THRESHOLD (default 0.60). If no chunk
       passes, the gatekeeper returns the fixed message "Not Found in
       Documents" and the generator LLM is never called.

Configuration comes from environment variables (see .env.example). The judge
prompt lives in a separate file (PROMPT_FILE). No secrets and no prompt text
are hardcoded in this script.

Usage:
    python jev_rag_filter.py --prompt "What is the refund policy?"
    python jev_rag_filter.py --prompt "..." --repeat 10 --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

LOGGER = logging.getLogger("jev_rag_filter")

# Key under which the yes/no question is sent to, and read back from, the model.
QUESTION_KEY = "has_answer"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or invalid."""


class JudgeError(RuntimeError):
    """Raised when the judge backend fails or returns an unusable answer."""


def _require(name: str) -> str:
    """Return a required environment variable or raise ConfigError."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    """All runtime settings, loaded once from the environment."""

    qdrant_url: str  # empty when qdrant_local_path is used
    qdrant_local_path: str | None  # no-server mode: data stored in this folder
    qdrant_api_key: str | None
    qdrant_collection: str
    qdrant_top_k: int
    qdrant_text_fields: tuple[str, ...]  # empty tuple = use the whole payload
    embedding_model: str
    query_prefix: str
    hf_token: str | None  # Hugging Face token for gated models
    backend: str  # "api" or "local"
    api_url: str
    api_model: str
    api_key: str | None
    api_timeout: float
    api_max_retries: int
    local_model_id: str
    threshold: float
    not_found_message: str
    prompt_file: Path

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config from environment variables, loading .env if present."""
        try:
            from dotenv import load_dotenv

            # override=True: values in .env win over stale system variables.
            load_dotenv(override=True)
        except ImportError:
            LOGGER.debug("python-dotenv not installed; using process environment only")

        backend = os.environ.get("JEV_BACKEND", "local").strip().lower()
        if backend not in {"api", "local"}:
            raise ConfigError("JEV_BACKEND must be 'api' or 'local'")

        # Optional no-Docker mode: Qdrant runs inside Python and keeps data in a folder.
        local_path = os.environ.get("QDRANT_LOCAL_PATH", "").strip() or None

        threshold = float(os.environ.get("GATE_THRESHOLD", "0.60"))
        if not 0.0 <= threshold <= 1.0:
            raise ConfigError("GATE_THRESHOLD must be between 0 and 1")

        # The API key is only mandatory when the API backend is selected.
        api_key = os.environ.get("MODEL_ACCESS_KEY", "").strip() or None
        if backend == "api" and api_key is None:
            raise ConfigError("MODEL_ACCESS_KEY is required when JEV_BACKEND=api")

        return cls(
            qdrant_url="" if local_path else _require("QDRANT_URL"),
            qdrant_local_path=local_path,
            qdrant_api_key=os.environ.get("QDRANT_API_KEY", "").strip() or None,
            qdrant_collection=_require("QDRANT_COLLECTION"),
            qdrant_top_k=int(os.environ.get("QDRANT_TOP_K", "3")),
            qdrant_text_fields=tuple(
                f.strip()
                for f in os.environ.get("QDRANT_TEXT_FIELDS", "").split(",")
                if f.strip()
            ),
            embedding_model=_require("EMBEDDING_MODEL"),
            # Some embedding models (e.g. Gemma) need a prefix on queries.
            query_prefix=os.environ.get("EMBEDDING_QUERY_PREFIX", ""),
            hf_token=os.environ.get("HF_TOKEN", "").strip() or None,
            backend=backend,
            api_url=os.environ.get(
                "JEV_API_URL", "https://inference.do-ai.run/v1/systemone"
            ),
            api_model=os.environ.get("JEV_MODEL", "typesafe-jev-1.13.0"),
            api_key=api_key,
            api_timeout=float(os.environ.get("JEV_TIMEOUT_SECONDS", "30")),
            api_max_retries=int(os.environ.get("JEV_MAX_RETRIES", "2")),
            local_model_id=os.environ.get(
                "LOCAL_MODEL_ID", "manjunathshiva/opendecider-nano"
            ),
            threshold=threshold,
            not_found_message=os.environ.get(
                "NOT_FOUND_MESSAGE", "Not Found in Documents"
            ),
            prompt_file=Path(
                os.environ.get("PROMPT_FILE", "prompts/judge_instructions.txt")
            ),
        )


def load_instructions(path: Path) -> str:
    """Read the judge prompt from its own file (prompts are never inlined)."""
    if not path.is_file():
        raise ConfigError(f"Prompt file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ConfigError(f"Prompt file is empty: {path}")
    return text


# --------------------------------------------------------------------------
# Data containers
# --------------------------------------------------------------------------


@dataclass
class Passage:
    """One retrieved chunk plus the scores attached to it along the pipeline."""

    id: str
    text: str
    retrieval_score: float
    judge_probability: float | None = None


@dataclass
class GateResult:
    """Final outcome of the gatekeeper for one user prompt."""

    status: str  # "found" or "not_found"
    message: str | None
    passages: list[Passage] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Step 1: Query the vector database
# --------------------------------------------------------------------------


def payload_to_text(payload: dict, fields: tuple[str, ...]) -> str:
    """Turn a Qdrant payload into the passage text shown to the judge.

    If specific fields are configured, only those are used; otherwise every
    payload field is included. Each field becomes one 'name: value' line.
    """
    selected = {k: payload[k] for k in fields if k in payload} if fields else dict(payload)
    return "\n".join(f"{key}: {value}" for key, value in selected.items())


class Retriever:
    """Embeds the prompt and queries Qdrant."""

    def __init__(self, cfg: Config) -> None:
        # Heavy imports are deferred so the module can be imported (and the
        # threshold logic tested) without these packages installed.
        from qdrant_client import QdrantClient
        from sentence_transformers import SentenceTransformer

        self._cfg = cfg
        if cfg.qdrant_local_path:
            self._client = QdrantClient(path=cfg.qdrant_local_path)
        else:
            self._client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key)
        try:
            # The token is passed explicitly so it is always used for gated models.
            self._embedder = SentenceTransformer(cfg.embedding_model, token=cfg.hf_token)
        except Exception as exc:  # noqa: BLE001 - any load failure gets one clear message
            raise ConfigError(
                f"Could not load embedding model '{cfg.embedding_model}'. Check that "
                "HF_TOKEN is valid and that access to the model was granted. "
                f"Original error: {str(exc)[:300]}"
            ) from exc

    def retrieve(self, prompt: str) -> tuple[list[Passage], dict[str, float]]:
        """Return the top-k passages for the prompt and per-step timings."""
        t0 = time.perf_counter()
        # Same recipe as indexing: optional query prefix + normalized vector.
        vector = self._embedder.encode(
            self._cfg.query_prefix + prompt, normalize_embeddings=True
        ).tolist()
        t1 = time.perf_counter()
        response = self._client.query_points(
            collection_name=self._cfg.qdrant_collection,
            query=vector,
            limit=self._cfg.qdrant_top_k,
            with_payload=True,
        )
        t2 = time.perf_counter()

        passages: list[Passage] = []
        for point in response.points:
            text = payload_to_text(point.payload or {}, self._cfg.qdrant_text_fields)
            if not text:
                LOGGER.warning("Point %s has no usable payload text; skipped", point.id)
                continue
            passages.append(Passage(str(point.id), text, float(point.score)))

        timings = {"embed": (t1 - t0) * 1000, "qdrant": (t2 - t1) * 1000}
        return passages, timings


# --------------------------------------------------------------------------
# Step 2: Judge backends (API and local share one interface)
# --------------------------------------------------------------------------


class Judge(Protocol):
    """Anything that can score how likely a passage answers a question."""

    def score_many(self, question: str, passages: list[str]) -> list[float]:
        """Return one probability in [0, 1] per passage, in the same order."""
        ...


def _build_state(question: str, passage: str) -> dict[str, str]:
    """State object the decision model evaluates the yes/no question against."""
    return {"question": question, "passage": passage}


class ApiJudge:
    """TypeSafe Jev through the /v1/systemone endpoint (closed API)."""

    def __init__(self, cfg: Config, instructions: str) -> None:
        import requests  # imported here: only needed for the API backend

        self._requests = requests
        self._cfg = cfg
        self._instructions = instructions

    def _score_one(self, question: str, passage: str) -> float:
        """Send one request, retrying on rate limits and server errors."""
        payload = {
            "model": self._cfg.api_model,
            "state": _build_state(question, passage),
            "questions": {
                QUESTION_KEY: {"type": "noul", "instructions": self._instructions}
            },
        }
        headers = {
            "Authorization": f"Bearer {self._cfg.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self._cfg.api_max_retries + 1):
            try:
                resp = self._requests.post(
                    self._cfg.api_url, headers=headers, json=payload,
                    timeout=self._cfg.api_timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise JudgeError(f"Transient API error: HTTP {resp.status_code}")
                resp.raise_for_status()
                return float(resp.json()["answers"][QUESTION_KEY]["noul"])
            except (JudgeError, self._requests.RequestException,
                    KeyError, ValueError, TypeError) as exc:
                last_error = exc
                LOGGER.warning("Jev API attempt %d failed: %s", attempt + 1, exc)
                time.sleep(0.5 * (2 ** attempt))  # exponential backoff
        raise JudgeError(f"Jev API failed after retries: {last_error}")

    def score_many(self, question: str, passages: list[str]) -> list[float]:
        """Score all passages in parallel; the API is network-bound."""
        if not passages:
            return []
        with ThreadPoolExecutor(max_workers=len(passages)) as pool:
            return list(pool.map(lambda p: self._score_one(question, p), passages))


class LocalJudge:
    """Open-source OpenDecider model running on this machine.

    The model exposes the same System One interface as Jev, so the state and
    the yes/no question are identical to the API backend.
    """

    def __init__(self, cfg: Config, instructions: str) -> None:
        # Imported lazily: loading downloads the base model + adapter on first use.
        from opendecider import Noul, load

        self._noul_cls = Noul
        self._instructions = instructions
        LOGGER.info("Loading local model %s (first run downloads weights)...",
                    cfg.local_model_id)
        self._model = load(cfg.local_model_id)

    def score_many(self, question: str, passages: list[str]) -> list[float]:
        """Score passages one by one (a single local GPU gains little from threads)."""
        scores: list[float] = []
        for passage in passages:
            try:
                result = self._model.system_one(
                    _build_state(question, passage),
                    {QUESTION_KEY: self._noul_cls(self._instructions)},
                )
                scores.append(float(result["answers"][QUESTION_KEY]["noul"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise JudgeError(f"Unexpected local model output: {exc}") from exc
        return scores


def build_judge(cfg: Config, instructions: str) -> Judge:
    """Pick the judge backend selected by JEV_BACKEND."""
    if cfg.backend == "api":
        return ApiJudge(cfg, instructions)
    return LocalJudge(cfg, instructions)


# --------------------------------------------------------------------------
# Step 3: Threshold logic (pure function, easy to unit test)
# --------------------------------------------------------------------------


def apply_threshold(passages: list[Passage], probabilities: list[float],
                    threshold: float) -> list[Passage]:
    """Keep passages whose probability is strictly greater than the threshold.

    A probability equal to the threshold is rejected, as the task requires
    (> 0.60 passes; <= 0.60 does not). Survivors are sorted best-first.
    """
    kept: list[Passage] = []
    for passage, probability in zip(passages, probabilities):
        passage.judge_probability = probability
        if probability > threshold:
            kept.append(passage)
    kept.sort(key=lambda p: p.judge_probability or 0.0, reverse=True)
    return kept


# --------------------------------------------------------------------------
# Gatekeeper: ties the three steps together
# --------------------------------------------------------------------------


def run_gatekeeper(prompt: str, cfg: Config, retriever: Retriever,
                   judge: Judge) -> GateResult:
    """Run retrieval, judging and thresholding for one prompt."""
    passages, timings = retriever.retrieve(prompt)

    t0 = time.perf_counter()
    probabilities = judge.score_many(prompt, [p.text for p in passages])
    timings["judge"] = (time.perf_counter() - t0) * 1000

    kept = apply_threshold(passages, probabilities, cfg.threshold)
    timings["total"] = sum(timings.values())

    if not kept:
        # Nothing answers the question: stop here, the generator is not called.
        return GateResult("not_found", cfg.not_found_message, passages, timings)
    return GateResult("found", None, kept, timings)


# --------------------------------------------------------------------------
# Command line interface
# --------------------------------------------------------------------------


def _print_latency_summary(runs: list[GateResult]) -> None:
    """Print median per-step latency across repeated runs."""
    steps = ["embed", "qdrant", "judge", "total"]
    print("\nMedian latency over %d run(s), in ms:" % len(runs))
    for step in steps:
        values = [r.timings_ms[step] for r in runs]
        print(f"  {step:<7} {statistics.median(values):8.1f}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Smart Gatekeeper for RAG")
    parser.add_argument("--prompt", required=True, help="User question to test")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Repeat the run N times to measure latency")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        cfg = Config.from_env()
        instructions = load_instructions(cfg.prompt_file)
        retriever = Retriever(cfg)
        judge = build_judge(cfg, instructions)

        runs = [run_gatekeeper(args.prompt, cfg, retriever, judge)
                for _ in range(max(1, args.repeat))]
    except (ConfigError, JudgeError) as exc:
        # Fail loudly: a judge outage must not look like "Not Found".
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    result = runs[-1]
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    elif result.status == "not_found":
        print(result.message)
    else:
        for passage in result.passages:
            print(f"[p={passage.judge_probability:.2f}] {passage.text}\n")

    if args.repeat > 1:
        _print_latency_summary(runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
