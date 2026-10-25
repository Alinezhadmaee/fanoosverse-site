"""Composite-value splitting (R-composite).

One value = one concept. ``Python and SQL`` and ``React, Vue`` become two
items; whitelisted pairs that are legitimately one concept (``CI/CD``,
``A/B Testing``, ``ML/AI``) stay whole. Values that resolve as a whole
(checked via ``keep_whole``) are never split — this protects entries like
"onsite and remote" (hybrid) whose split parts would mean something else.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from normalizer_agent.text.unify import unify_key

_SPLIT_RE = re.compile(
    r"""
    [,،;؛]                      # comma / semicolon (western + persian)
    | \s+/\s* | /\s+            # slash with whitespace on at least one side
    | \s+و\s+                   # standalone persian "and"
    | \s+&\s+                   # standalone ampersand
    | \n+                       # line breaks
    """,
    re.VERBOSE,
)


def split_composite(
    value: str,
    whitelist: set[str] | frozenset[str],
    keep_whole: Callable[[str], bool] | None = None,
) -> list[str]:
    """Split ``value`` into single-concept parts.

    ``whitelist`` holds unified-lowercase forms that must never be split
    (checked on the whole value AND on each candidate part, so ``CI/CD``
    survives even when it appears inside a larger composite).
    ``keep_whole`` is consulted before splitting (vocabulary hit on the whole
    value means the value is a known single concept).
    """

    whole_key = unify_key(value)
    if whole_key in whitelist:
        return [value.strip()]
    if keep_whole is not None and keep_whole(whole_key):
        return [value.strip()]

    parts = [part.strip(" \t-") for part in _SPLIT_RE.split(value)] or [value.strip()]
    kept: list[str] = []
    for part in parts:
        if not part:
            continue
        if unify_key(part) in whitelist:
            kept.append(part)
            continue
        # a slash with no spaces ("QLoRA/LoRA") inside a part -> split anyway
        for sub in re.split(r"(?<=[^\s])/(?=[^\s])", part):
            sub = sub.strip()
            if sub and unify_key(sub) not in (
                "",
                None,
            ):
                kept.append(sub)
    # dedupe, preserving first occurrence
    seen: set[str] = set()
    unique: list[str] = []
    for part in kept:
        key = unify_key(part)
        if key and key not in seen:
            seen.add(key)
            unique.append(part)
    return unique
