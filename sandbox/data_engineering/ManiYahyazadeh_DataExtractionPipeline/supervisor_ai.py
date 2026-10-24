"""
AI supervisor: checks one extracted record against the raw ad text.

Prompt and decision format come from Ahmadreza Hashemi's supervisor agent.
The prompt is in supervisor_prompt.md. Model and provider are set with SUPERVISOR_* in .env
(see llm_config.py), so retry rules, token counting and error kinds are shared with the extractor.
"""
import json
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llm_config import SUPERVISOR, LLMError, call_llm

PROMPT = Path(__file__).with_name("supervisor_prompt.md").read_text(encoding="utf-8")
if not PROMPT.strip():
    raise RuntimeError("supervisor_prompt.md is empty")

ATTEMPTS = 3


class SupervisorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_approved: bool = Field(
        description="True if the extracted fields perfectly match the initial text without errors, omissions, or hallucinations. False otherwise."
    )
    rejection_reason: str = Field(
        description="If rejected, provide a highly specific explanation of exactly which fields are wrong or missing. If approved, return an empty string."
    )


def ai_review(completed_fields: dict, initial_data: str) -> SupervisorDecision:
    """
    Ask the AI supervisor and return its decision.
    Network errors and malformed replies are retried. If it still fails, LLMError("fatal") is raised:
    a broken supervisor must not be mistaken for a rejected extraction.
    """
    user_message = f"""
Initial Data (Raw Text):
{initial_data}

Completed Fields (Parsed JSON):
{json.dumps(completed_fields, ensure_ascii=False, indent=2)}
"""
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            raw = call_llm(PROMPT, user_message, SupervisorDecision.model_json_schema(),
                           1500, profile=SUPERVISOR)
            return SupervisorDecision.model_validate_json(raw)
        except ValidationError as e:
            last = f"malformed supervisor reply: {str(e)[:200]}"
        except LLMError as e:
            if e.kind != "transient":   # bad key, no credit, cut off: a retry will not help
                raise
            last = str(e)
        if attempt < ATTEMPTS:
            time.sleep(2 ** attempt)
    raise LLMError(f"Supervisor unavailable: {last[:300]}", "fatal")
