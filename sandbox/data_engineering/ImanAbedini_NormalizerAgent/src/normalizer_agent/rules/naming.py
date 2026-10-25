"""Canonical naming rules (R-naming).

Concepts in Title Case, well-known acronyms stay uppercase, enum values in
lower snake_case. Version numbers and proficiency levels are stripped from
canonical names ("Python3" -> "Python"); the removed part is reported for the
audit trail.
"""

from __future__ import annotations

import re

_KNOWN_ACRONYMS = {
    "ai",
    "ml",
    "dl",
    "nlp",
    "llm",
    "llms",
    "rag",
    "sql",
    "nosql",
    "api",
    "apis",
    "ci",
    "cd",
    "cicd",
    "etl",
    "elt",
    "kpi",
    "okr",
    "crm",
    "erp",
    "saas",
    "paas",
    "iaas",
    "aws",
    "gcp",
    "sdk",
    "cli",
    "gui",
    "ui",
    "ux",
    "css",
    "html",
    "http",
    "https",
    "rest",
    "grpc",
    "json",
    "xml",
    "yaml",
    "csv",
    "pdf",
    "ocr",
    "gan",
    "cnn",
    "rnn",
    "lstm",
    "gru",
    "bert",
    "gpt",
    "sft",
    "dpo",
    "rlhf",
    "mlops",
    "devops",
    "dataops",
    "bi",
    "et",
    "qa",
    "seo",
    "sem",
    "b2b",
    "b2c",
    "stt",
    "tts",
    "asr",
    "irl",
    "po",
    "pm",
    "hr",
    "it",
    "iot",
    "ar",
    "vr",
    "nft",
    "gpu",
    "cpu",
    "ram",
    "tpu",
    "orm",
    "mvc",
    "oop",
    "dsl",
    "sse",
    "jwt",
    "ssl",
    "tls",
    "ssh",
    "smtp",
    "dns",
    "cdn",
    "wasm",
    "pwa",
    "spa",
    "er",
}

_VERSION_SUFFIX_RE = re.compile(
    r"[\s._-]*(?:\d+(?:\.\d+)+|v\d+(?:\.\d+)*|3|2|2019|2020|2021|2022)$"
)

_PROFICIENCY_RE = re.compile(
    r"\((?:[^()]*؟?)[^()]*\)|"
    r"(?:\s+-\s+)?(?:basic|intermediate|upper[\s-]?intermediate|advanced|"
    r"native|fluent|beginner|elementary|proficient|mastery)\b.*$",
    flags=re.IGNORECASE,
)


def strip_version(raw: str) -> tuple[str, str | None]:
    """Remove a trailing version marker ("Python3" -> ("Python", "3"))."""

    match = _VERSION_SUFFIX_RE.search(raw)
    if match is None:
        return raw.strip(), None
    return raw[: match.start()].strip(), raw[match.start() :].strip()


def _display_form(word: str) -> str:
    lowered = word.casefold().strip(".")
    if lowered in _KNOWN_ACRONYMS:
        return lowered.upper()
    if word.isupper():  # user-provided acronym we do not know: keep as-is
        return word
    if any(ch.isdigit() for ch in word):
        return word  # C#, 3D, Node.js-like tokens keep their shape
    special = {
        "javascript": "JavaScript",
        "typescript": "TypeScript",
        "node.js": "Node.js",
        "next.js": "Next.js",
        "vue.js": "Vue.js",
        "power bi": "Power BI",
    }
    if lowered in special:
        return special[lowered]
    return word[:1].upper() + word[1:].lower()


def title_case(raw: str) -> str:
    """Title Case for concept names, respecting acronyms and separators."""

    cleaned = raw.strip()
    parts = cleaned.split()
    return " ".join(_display_form(part) for part in parts if part)


def is_title_case(value: str) -> bool:
    words = value.split()
    return bool(words) and all(
        word[0].isupper() or word[:1].isdigit() or not word[0].isalpha() for word in words
    )


def is_snake_case(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)*", value))


def strip_proficiency(raw: str) -> tuple[str, str | None]:
    """Remove language-proficiency tails ("English - Upper Intermediate")."""

    match = _PROFICIENCY_RE.search(raw)
    if match is None:
        return raw.strip(), None
    return raw[: match.start()].strip(" -,؛،"), raw[match.start() :].strip(" -")
