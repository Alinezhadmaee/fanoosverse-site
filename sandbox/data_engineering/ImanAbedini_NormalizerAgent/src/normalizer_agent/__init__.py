"""Fanos LLM Normalizer Agent.

Canonicalizes raw job-advertisement field values (mostly Persian, some English)
into the Fanos canonical form, deterministic-first with an LLM fallback layer.
"""

from __future__ import annotations

__version__ = "0.1.0"

PROMPT_VERSION = "v1"
