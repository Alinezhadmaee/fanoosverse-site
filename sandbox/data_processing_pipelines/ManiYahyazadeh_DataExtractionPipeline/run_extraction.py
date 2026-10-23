"""
Fanoos extraction pipeline: scraped raw text in, validated JobAd records out (JSONL).

How it works:
    1. Every raw text is numbered (job_bot_0001, ...) and saved first. Duplicates are skipped.
    2. Pending ads are cut into small batches that fit the token budget.
    3. A batch must reach a final state (processed or dropped) before the next batch starts.
    4. Each ad goes through a loop: LLM, Pydantic, optional supervisor, and back to the LLM
       with the problems. After the last round the ad is dropped (its raw text stays saved).

Run:
    python run_extraction.py scraped.jsonl --collector bot    add new ads and process them
    python run_extraction.py                                  continue the pending ads
    python run_extraction.py --max-ads 10                     process only 10 ads (cheap test)
    python run_extraction.py --review                         also check every result with the AI supervisor
    python run_extraction.py in.jsonl --collected-at 2026-08-30   date the ads were collected (default: today)

Input file: JSONL. Each line is a JSON string, or an object with a "text" key.
Output folder (default data/): raw_ads.jsonl, processed.jsonl, dropped.jsonl,
duplicates.txt, usage.jsonl
"""
import argparse
import json
import logging
import math
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
from typing import Callable

from pydantic import ConfigDict, ValidationError, create_model

import llm_config
from ad_store import Ad, AdStore, append_line
from llm_config import LLMError, call_llm
from schema import JobAd, SourceType

log = logging.getLogger("fanoos.pipeline")

PROMPT_PATH = Path(__file__).with_name("extraction_prompt.md")

BATCH_NOTE = """

Batch mode: you receive several postings, each with a posting id.
Return one JSON object {"items": [...]} with one item per posting, in the same order.
Each item has "document_id" set to the posting id, plus the 30 fields described above.
If a posting lists problems from a previous answer, fix those problems and keep the rest."""

# The LLM fills only the semantic fields. Python adds the metadata.
METADATA = {"document_id", "collector", "source_type", "collected_at", "text", "content_hash", "version"}
_llm_fields = {n: (f.annotation, ...) for n, f in JobAd.model_fields.items() if n not in METADATA}
BatchItem = create_model("BatchItem", __config__=ConfigDict(extra="forbid"),
                         document_id=(str, ...), **_llm_fields)
BatchOutput = create_model("BatchOutput", __config__=ConfigDict(extra="forbid"),
                           items=(list[BatchItem], ...))
OUTPUT_SCHEMA = BatchOutput.model_json_schema()


@dataclass
class Settings:
    data_dir: str = "data"
    version: str = "v2"
    max_rounds: int = 4              # LLM and Pydantic rounds per ad
    max_ads_per_batch: int = 5
    input_token_budget: int = 24000  # estimated input tokens per request
    output_tokens_per_ad: int = 2000
    output_tokens_max: int = 10000
    ad_max_tokens: int = 6000        # longer ads are dropped
    chars_per_token: float = 2.0     # a low value is safe for Persian text
    llm_attempts: int = 3            # tries for network or rate limit errors
    max_llm_calls: int = 500         # hard stop for one run, protects the budget

    @classmethod
    def from_env(cls) -> "Settings":
        """Every setting can be changed with an env var like FANOOS_MAX_ROUNDS."""
        settings = cls()
        for f in fields(cls):
            value = os.getenv("FANOOS_" + f.name.upper())
            if value:
                setattr(settings, f.name, type(getattr(settings, f.name))(value))
        return settings


@dataclass
class Feedback:
    """Answer of the supervisor. Every part is optional."""
    error_type: str | None = None
    explanation: str | None = None
    valid: bool | None = None

    def problem(self) -> str | None:
        """Return the problem text, or None when the ad is accepted."""
        if self.valid is True:
            return None
        text = ": ".join(p for p in (self.error_type, self.explanation) if p)
        if text:
            return "Reviewer: " + text
        return "Reviewer rejected the ad." if self.valid is False else None


# The supervisor gets a valid JobAd and returns Feedback (or None for no comment).
Review = Callable[[JobAd], Feedback | None]

def build_review() -> Review:
    """Connect the AI supervisor (imported here, so the pipeline also works without it)."""
    from supervisor_ai import ai_review

    def review(job: JobAd) -> Feedback:
        decision = ai_review(job.model_dump(mode="json", exclude=METADATA), job.text)
        return Feedback(valid=decision.is_approved, error_type="ai_supervisor",
                        explanation=decision.rejection_reason or None)
    return review


class FatalError(Exception):
    """The run cannot continue (bad key, no credit, call limit, supervisor crash)."""


class OutputCutOff(Exception):
    """The model reply was cut off because the output limit was reached."""


def describe(error: ValidationError) -> list[str]:
    """Short readable list of Pydantic problems."""
    items = []
    for err in error.errors()[:10]:
        where = ".".join(str(p) for p in err["loc"]) or "item"
        items.append(f"{where}: {err['msg']}")
    return items


def rule_problems(job: JobAd) -> list[str]:
    """Checks that the schema types cannot do. They match the prompt checklist."""
    out = []
    if job.salary_min is not None and job.salary_max is not None and job.salary_min > job.salary_max:
        out.append("salary_min is bigger than salary_max")
    if job.age_min is not None and job.age_max is not None and job.age_min > job.age_max:
        out.append("age_min is bigger than age_max")
    if job.company_size_min is not None and job.company_size_max is not None \
            and job.company_size_min > job.company_size_max:
        out.append("company_size_min is bigger than company_size_max")
    for name in ("experience_years_min", "salary_min", "salary_max", "age_min", "age_max",
                 "company_size_min", "company_size_max", "internship_duration_months"):
        value = getattr(job, name)
        if value is not None and value < 0:
            out.append(f"{name} is negative")
    if not job.role_title.strip() or not job.role_normalized.strip():
        out.append("role_title and role_normalized must not be empty")
    if not job.work_mode:
        out.append('work_mode must not be empty, use ["unknown"]')
    return out


class Pipeline:
    def __init__(self, store: AdStore, settings: Settings, llm=call_llm,
                 review: Review | None = None, sleep=time.sleep) -> None:
        self.store = store
        self.cfg = settings
        self.llm = llm
        self.review = review
        self.sleep = sleep
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8") + BATCH_NOTE
        self.prompt_tokens = self.tokens(self.system_prompt)
        self.stats = Counter()
        if self.prompt_tokens + settings.ad_max_tokens + 500 > settings.input_token_budget:
            raise ValueError("input_token_budget is too small for ad_max_tokens")
        if settings.output_tokens_per_ad > settings.output_tokens_max:
            raise ValueError("output_tokens_per_ad is bigger than output_tokens_max")

    # Token budget

    def tokens(self, text: str) -> int:
        return math.ceil(len(text) / self.cfg.chars_per_token)

    def ad_cost(self, ad: Ad, notes: dict) -> int:
        """Estimated input tokens of one ad, with its previous answer when it is retried."""
        cost = self.tokens(ad.text) + 80
        previous = notes.get(ad.document_id)
        if previous and isinstance(previous[0], dict):
            cost += self.tokens(json.dumps(previous[0], ensure_ascii=False))
        return cost

    def make_batches(self, ads: list[Ad], notes: dict) -> list[list[Ad]]:
        """Group ads in order. An ad is never split between two batches."""
        cfg = self.cfg
        batches, current, used = [], [], self.prompt_tokens
        for ad in ads:
            cost = self.ad_cost(ad, notes)
            full = (len(current) >= cfg.max_ads_per_batch
                    or used + cost > cfg.input_token_budget
                    or (len(current) + 1) * cfg.output_tokens_per_ad > cfg.output_tokens_max)
            if current and full:
                batches.append(current)
                current, used = [], self.prompt_tokens
            current.append(ad)
            used += cost
        if current:
            batches.append(current)
        return batches

    # LLM calls

    def call_llm_safe(self, user_prompt: str, count: int) -> str:
        """One LLM call with retry for transient errors."""
        last = ""
        for attempt in range(1, self.cfg.llm_attempts + 1):
            if self.stats["llm_calls"] >= self.cfg.max_llm_calls:
                raise FatalError(f"Call limit reached ({self.cfg.max_llm_calls}). Run again to continue.")
            self.stats["llm_calls"] += 1
            try:
                return self.llm(self.system_prompt, user_prompt, OUTPUT_SCHEMA,
                                count * self.cfg.output_tokens_per_ad)
            except LLMError as e:
                kind = getattr(e, "kind", "transient")
                if kind == "cut_off":
                    raise OutputCutOff(str(e)) from e
                if kind == "fatal":
                    raise FatalError(str(e)) from e
                last = str(e)
                log.warning("LLM call failed (try %d of %d): %s", attempt, self.cfg.llm_attempts, last[:200])
                if attempt < self.cfg.llm_attempts:
                    self.sleep(2 ** attempt)
        raise FatalError(f"LLM is not available: {last[:300]}")

    @staticmethod
    def build_message(ads: list[Ad], notes: dict) -> str:
        parts = []
        for ad in ads:
            parts.append(f"Posting id: {ad.document_id}\n<<<\n{ad.text}\n>>>")
            if ad.document_id in notes:
                previous, problems = notes[ad.document_id]
                if isinstance(previous, dict):
                    parts.append("Your previous answer for this posting:\n"
                                 + json.dumps(previous, ensure_ascii=False))
                parts.append("Problems to fix:\n" + "\n".join("- " + p for p in problems))
        return "\n\n".join(parts)

    def ask(self, ads: list[Ad], notes: dict) -> dict:
        """Ask the LLM about some ads. Returns id -> item dict, or id -> problem text."""
        results = {}
        for batch in self.make_batches(ads, notes):
            results.update(self.ask_batch(batch, notes))
        return results

    def ask_batch(self, batch: list[Ad], notes: dict) -> dict:
        try:
            raw = self.call_llm_safe(self.build_message(batch, notes), len(batch))
        except OutputCutOff:
            if len(batch) == 1:
                return {batch[0].document_id: "The model output was cut off. Make the answer shorter."}
            log.info("Output was cut off, splitting a batch of %d ads", len(batch))
            self.stats["splits"] += 1
            middle = len(batch) // 2
            return {**self.ask_batch(batch[:middle], notes), **self.ask_batch(batch[middle:], notes)}
        try:
            items = json.loads(raw)["items"]
            if not isinstance(items, list):
                raise TypeError
        except (json.JSONDecodeError, KeyError, TypeError):
            return {ad.document_id: 'The reply is not a JSON object with an "items" list.' for ad in batch}
        found = {i["document_id"]: i for i in items if isinstance(i, dict) and "document_id" in i}
        return {ad.document_id: found.get(ad.document_id) for ad in batch}

    # Checks

    def check(self, ad: Ad, item) -> tuple[JobAd | None, list[str]]:
        """Run Pydantic, the extra rules and the supervisor. Returns the record or the problems."""
        if isinstance(item, str):
            return None, [item]
        if item is None:
            return None, ["The posting is missing from the model output."]
        try:
            extracted = BatchItem.model_validate(item)
            job = JobAd.model_validate({
                **extracted.model_dump(exclude={"document_id"}),
                "document_id": ad.document_id,
                "collector": ad.collector,
                "source_type": SourceType.JOB_POSTING,
                "collected_at": date.fromisoformat(ad.collected_at),
                "text": ad.text,
                "content_hash": ad.content_hash,
                "version": self.cfg.version,
            })
        except ValidationError as e:
            return None, describe(e)
        problems = rule_problems(job)
        if problems:
            return None, problems
        if self.review:
            try:
                problem = (self.review(job) or Feedback()).problem()
            except Exception as e:
                raise FatalError(f"Supervisor failed: {type(e).__name__}: {e}") from e
            if problem:
                return None, [problem]
        return job, []

    # Main loop

    def run_batch(self, batch: list[Ad]) -> None:
        """Work on one batch until every ad is processed or dropped."""
        waiting = {ad.document_id: ad for ad in batch}
        notes: dict = {}
        for round_number in range(1, self.cfg.max_rounds + 1):
            if not waiting:
                return
            log.info("Round %d: %d ad(s)", round_number, len(waiting))
            results = self.ask(list(waiting.values()), notes)
            for ad in list(waiting.values()):
                item = results.get(ad.document_id)
                job, problems = self.check(ad, item)
                if job:
                    self.store.save_processed(ad.document_id, json.loads(job.model_dump_json()))
                    self.stats["processed"] += 1
                    del waiting[ad.document_id]
                else:
                    notes[ad.document_id] = (item, problems)
                    log.info("%s has problems: %s", ad.document_id, "; ".join(problems)[:300])
        for ad in waiting.values():
            self.store.save_dropped(ad, "max_rounds", self.cfg.max_rounds, notes[ad.document_id][1])
            self.stats["dropped"] += 1
            log.warning("%s dropped after %d rounds", ad.document_id, self.cfg.max_rounds)

    def run(self, max_ads: int | None = None) -> Counter:
        """Process the pending ads. Progress is on disk, so it is safe to stop and run again."""
        queue = []
        for ad in self.store.pending():
            if max_ads is not None and len(queue) >= max_ads:
                break
            if self.tokens(ad.text) > self.cfg.ad_max_tokens:
                self.store.save_dropped(ad, "too_long", 0, [])
                self.stats["dropped"] += 1
                log.warning("%s dropped: text is too long", ad.document_id)
            else:
                queue.append(ad)
        log.info("%d ad(s) to process", len(queue))
        try:
            for batch in self.make_batches(queue, {}):
                log.info("Batch %s to %s", batch[0].document_id, batch[-1].document_id)
                self.run_batch(batch)
        except FatalError as e:
            self.stats["stopped"] = 1
            log.error("Stopped: %s", e)
        self.stats["left"] = len(self.store.pending())
        return self.stats


def read_input(path: str) -> list:
    texts = []
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                sys.exit(f"Input line {number} is not valid JSON.")
            texts.append(value.get("text") if isinstance(value, dict) else value)
    return texts


def main() -> None:
    parser = argparse.ArgumentParser(description="Fanoos extraction pipeline")
    parser.add_argument("input", nargs="?", help="JSONL file with scraped texts")
    parser.add_argument("--collector", default="bot", help="short source name used in ids")
    parser.add_argument("--data-dir", help="output folder")
    parser.add_argument("--max-ads", type=int, help="process only this many pending ads")
    parser.add_argument("--review", action="store_true", help="check every result with the AI supervisor")
    parser.add_argument("--collected-at", help="collection date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    settings = Settings.from_env()
    if args.data_dir:
        settings.data_dir = args.data_dir

    store = AdStore(settings.data_dir)
    if args.input:
        counts = store.add_ads(read_input(args.input), args.collector, args.collected_at)
        log.info("Input: %d new, %d duplicate, %d empty", counts["added"], counts["duplicates"], counts["empty"])

    try:
        stats = Pipeline(store, settings, review=build_review() if args.review else None).run(args.max_ads)
    except KeyboardInterrupt:
        sys.exit("Stopped by user. Progress is saved, run again to continue.")

    usage = {"date": date.today().isoformat(), **stats, **llm_config.USAGE}
    append_line(store.folder / "usage.jsonl", json.dumps(usage, ensure_ascii=False))
    log.info("Done: %d processed, %d dropped, %d left, %d LLM calls, %d input and %d output tokens",
             stats["processed"], stats["dropped"], stats["left"], stats["llm_calls"],
             llm_config.USAGE["input_tokens"], llm_config.USAGE["output_tokens"])
    sys.exit(1 if stats["stopped"] else 0)


if __name__ == "__main__":
    main()
