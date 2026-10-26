"""CLI entry points for the Fanos Normalizer.

Commands:
  normalizer run [--input FILE] [--output FILE] [--config-dir DIR]
  normalizer llm-check [--config-dir DIR]
  normalizer validate-config [--config-dir DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from normalizer_agent import __version__
from normalizer_agent.interface.api import Normalizer
from normalizer_agent.llm.base import LLMRequest
from normalizer_agent.llm.router import build_router
from normalizer_agent.observability.logging import setup_logging
from normalizer_agent.schemas.job_ad import JobAd
from normalizer_agent.settings import load_app_config
from normalizer_agent.startup_check import check_fields_config

logger = logging.getLogger("normalizer_agent.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="normalizer",
        description="Fanos LLM Normalizer Agent CLI",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1) run
    p_run = subparsers.add_parser("run", help="Normalize raw records from a file or stdin")
    p_run.add_argument(
        "--input",
        "-i",
        type=Path,
        default=None,
        help="Input JSON or JSONL file (defaults to stdin)",
    )
    p_run.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output JSONL file (defaults to stdout)",
    )
    p_run.add_argument(
        "--config-dir",
        "-c",
        type=Path,
        default=Path("config"),
        help="Directory containing YAML configurations (default: config)",
    )
    p_run.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    # 2) llm-check
    p_llm = subparsers.add_parser("llm-check", help="Verify configured LLM providers with a probe")
    p_llm.add_argument(
        "--config-dir",
        "-c",
        type=Path,
        default=Path("config"),
        help="Directory containing YAML configurations",
    )

    # 3) validate-config
    p_val = subparsers.add_parser(
        "validate-config", help="Validate YAML configurations against schema"
    )
    p_val.add_argument(
        "--config-dir",
        "-c",
        type=Path,
        default=Path("config"),
        help="Directory containing YAML configurations",
    )

    return parser


def _read_records(input_path: Path | None) -> list[dict[str, Any]]:
    if input_path is not None:
        text = input_path.read_text(encoding="utf-8").strip()
    else:
        text = sys.stdin.read().strip()

    if not text:
        return []

    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            return [data]
        except json.JSONDecodeError:
            pass

    records = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


async def _run_command(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    config_dir = args.config_dir
    records = _read_records(args.input)
    if not records:
        print("No records found to process.", file=sys.stderr)
        return 0

    normalizer = await Normalizer.from_config(config_dir)
    out_file = args.output.open("w", encoding="utf-8") if args.output else sys.stdout

    try:
        results = await normalizer.normalize_batch(records)
        for res in results:
            payload = res.model_dump(mode="json")
            out_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if args.output:
            out_file.close()

        # Print metrics summary to stderr
        print(
            json.dumps({"summary": normalizer.metrics.as_dict()}, ensure_ascii=False),
            file=sys.stderr,
        )
    finally:
        await normalizer.aclose()

    return 0


async def _llm_check_command(args: argparse.Namespace) -> int:
    setup_logging("INFO")
    config = load_app_config(args.config_dir)
    print(f"Loaded config from {args.config_dir}. Enabled providers:")
    router = build_router(config.llm, config.pipeline)
    test_req = LLMRequest(
        system='Answer in JSON: {"ping": "pong"}',
        user="ping",
        expect_json=True,
    )
    for entry in router._entries:
        p_name = entry.config.name
        p_type = entry.config.type
        p_model = entry.config.model
        print(f" - Testing provider '{p_name}' ({p_type} / {p_model})... ", end="", flush=True)
        try:
            resp = await entry.provider.complete(test_req)
            tokens = f"{resp.input_tokens}+{resp.output_tokens}"
            print(f"OK (latency={resp.latency_ms:.1f}ms, tokens={tokens})")
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED: {exc}")
    await router.aclose()
    return 0


def _validate_config_command(args: argparse.Namespace) -> int:
    try:
        config = load_app_config(args.config_dir)
        check_fields_config(config.fields, JobAd)
        print(f"Config in '{args.config_dir}' is valid.")
        print(f" - Schema fields matched: {len(config.fields.fields)}")
        print(f" - Enabled providers: {[p.name for p in config.llm.enabled_providers()]}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Configuration validation error: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        exit_code = asyncio.run(_run_command(args))
    elif args.command == "llm-check":
        exit_code = asyncio.run(_llm_check_command(args))
    elif args.command == "validate-config":
        exit_code = _validate_config_command(args)
    else:
        parser.print_help()
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
