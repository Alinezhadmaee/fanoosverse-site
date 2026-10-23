"""
LLM settings and the one function that talks to the LLM.

Change provider or model: edit .env. Nothing else needs to change.
Test the connection: python llm_config.py

Every LLM role has a profile (model, provider, key, settings):
    EXTRACTOR  reads LLM_*        SUPERVISOR  reads SUPERVISOR_*
A role that is missing a setting copies it from the extractor.
A key is copied only when the provider (base url) is the same, never across providers.

.env:
    LLM_EXTRACTOR_GAPGPT=<api key>                (LLM_EXTRACTOR also works)
    LLM_BASE_URL=https://api.gapgpt.app/v1        (default)
    LLM_MODEL=gpt-4o                              (default)
Optional:
    LLM_MAX_TOKENS=3500
    LLM_MAX_TOKENS_PARAM=max_tokens               (or max_completion_tokens)
    LLM_TEMPERATURE=0                             ("none" means do not send it)
    LLM_STRUCTURED_MODE=json_schema               (or json_object)
    LLM_TIMEOUT=180                               (seconds)
    LLM_EXTRA_BODY={"provider":{"require_parameters":true}}
Supervisor (same names with the SUPERVISOR_ prefix):
    SUPERVISOR_API_KEY=<api key>
    SUPERVISOR_BASE_URL=https://api.avalai.ir/v1
    SUPERVISOR_MODEL=<model id>
    SUPERVISOR_STRUCTURED_MODE=json_object
"""
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)


@dataclass(frozen=True)
class LLMProfile:
    """Everything that defines one LLM role (extractor, supervisor, normalizer ...)."""
    name: str
    api_key: str
    base_url: str
    model: str
    max_tokens: int = 3500
    max_tokens_param: str = "max_tokens"
    temperature: float | None = 0.0
    structured_mode: str = "json_schema"      # or "json_object"
    timeout: float = 180.0
    extra_body: dict | None = None

    @classmethod
    def from_env(cls, prefix: str, name: str, base: "LLMProfile | None" = None,
                 api_key: str = "") -> "LLMProfile":
        """Read PREFIX_MODEL, PREFIX_BASE_URL ... Missing values come from `base`, then defaults."""
        def get(key: str, fallback):
            value = (os.getenv(f"{prefix}_{key}") or "").strip()
            return value if value else fallback

        b = base
        temp = get("TEMPERATURE", None)
        extra = get("EXTRA_BODY", None)
        base_url = get("BASE_URL", b.base_url if b else "https://api.gapgpt.app/v1")
        # A key is copied from `base` only when the provider is the same.
        inherited_key = b.api_key if b and base_url == b.base_url else ""
        return cls(
            name=name,
            api_key=api_key or get("API_KEY", inherited_key),
            base_url=base_url,
            model=get("MODEL", b.model if b else "gpt-4o"),
            max_tokens=int(get("MAX_TOKENS", b.max_tokens if b else 3500)),
            max_tokens_param=get("MAX_TOKENS_PARAM", b.max_tokens_param if b else "max_tokens"),
            temperature=(b.temperature if b else 0.0) if temp is None
                        else (None if temp.lower() == "none" else float(temp)),
            structured_mode=get("STRUCTURED_MODE", b.structured_mode if b else "json_schema"),
            timeout=float(get("TIMEOUT", b.timeout if b else 180)),
            extra_body=(b.extra_body if b else None) if extra is None else json.loads(extra),
        )


EXTRACTOR = LLMProfile.from_env(
    "LLM", "extractor",
    api_key=(os.getenv("LLM_EXTRACTOR_GAPGPT_1") or os.getenv("LLM_EXTRACTOR") or "").strip())
SUPERVISOR = LLMProfile.from_env("SUPERVISOR", "supervisor", base=EXTRACTOR)

# Token use of all calls in this process: totals (the pipeline reads these) and one pair per role.
USAGE = Counter()

FATAL_STATUS = {400, 401, 402, 403, 404}


class LLMError(Exception):
    """
    Any LLM failure.
    kind = "transient" : network, rate limit or server error. Retry later.
    kind = "fatal"     : bad key, no credit or bad request. Retry will not help.
    kind = "cut_off"   : the answer hit the output limit.
    """

    def __init__(self, message: str, kind: str = "transient") -> None:
        super().__init__(message)
        self.kind = kind


def _kind_of(error: Exception) -> str:
    return "fatal" if getattr(error, "status_code", None) in FATAL_STATUS else "transient"


@lru_cache(maxsize=None)
def _client(base_url: str, api_key: str, timeout: float) -> OpenAI:
    """One client per provider and key, reused between calls (keeps connections open)."""
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)


def call_llm(system_prompt: str, user_prompt: str, schema: dict | None = None,
             max_tokens: int | None = None, profile: LLMProfile = EXTRACTOR) -> str:
    """Send a prompt and return the reply text. With a schema, the reply is JSON text.
    `profile` says which model, provider and settings to use (default: the extractor)."""
    p = profile
    if not p.api_key:
        raise LLMError(f"API key of '{p.name}' is not set in your .env file.", "fatal")

    kwargs = {
        "model": p.model,
        p.max_tokens_param: max_tokens or p.max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if p.temperature is not None:
        kwargs["temperature"] = p.temperature
    if p.extra_body:
        kwargs["extra_body"] = p.extra_body

    if schema:
        if p.structured_mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": schema},
            }
        elif p.structured_mode == "json_object":  # the schema goes into the prompt
            kwargs["messages"][0]["content"] += (
                "\n\nReturn ONE JSON object that follows this JSON Schema exactly:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
            kwargs["response_format"] = {"type": "json_object"}
        else:
            raise LLMError(f"Unknown structured mode of '{p.name}': {p.structured_mode}", "fatal")

    try:
        resp = _client(p.base_url, p.api_key, p.timeout).chat.completions.create(**kwargs)
    except Exception as e:
        raise LLMError(f"LLM call failed ({p.name}/{p.model}): {type(e).__name__}: {e}", _kind_of(e)) from e

    if resp.usage:
        used_in, used_out = resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0
        USAGE["input_tokens"] += used_in
        USAGE["output_tokens"] += used_out
        USAGE[f"{p.name}_input_tokens"] += used_in
        USAGE[f"{p.name}_output_tokens"] += used_out

    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise LLMError("Output was cut off.", "cut_off")
    if not choice.message.content:
        raise LLMError("LLM returned an empty reply.", "transient")
    return choice.message.content


if __name__ == "__main__":  # connection self-test
    from pydantic import BaseModel, ConfigDict

    class Mini(BaseModel):
        model_config = ConfigDict(extra="forbid")
        role_title: str
        seniority: list[str]
        salary_min: int | None

    print(f"endpoint={EXTRACTOR.base_url}  model={EXTRACTOR.model}  mode={EXTRACTOR.structured_mode}")
    ok = 0
    try:
        print("1 ping      :", call_llm("You are a test.", "Reply with exactly: OK", max_tokens=20))
        ok += 1
    except LLMError as e:
        print("1 FAILED    :", e)
    try:
        raw = call_llm("Extract the job info. Use null or [] if not in the text.",
                       "مهندس ارشد یادگیری ماشین، تهران، حقوق ۸۰ میلیون تومان",
                       Mini.model_json_schema())
        print("2 structured:", Mini.model_validate_json(raw).model_dump())
        ok += 1
    except Exception as e:
        print("2 FAILED    :", str(e)[:400])
        print("  hint: set LLM_STRUCTURED_MODE=json_object in .env and retry")
    print(f"Passed {ok} of 2")
    sys.exit(0 if ok == 2 else 1)