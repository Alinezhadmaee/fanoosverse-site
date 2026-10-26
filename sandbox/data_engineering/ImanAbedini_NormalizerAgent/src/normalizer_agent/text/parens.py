"""Parenthetical-remark removal (R-parenthesis).

Explanations inside parentheses are not part of the value; the removed text is
returned (character-unified) so it can be kept in the audit trail / evidence
notes.
"""

from __future__ import annotations

import re

from normalizer_agent.text.unify import unify_text

# ASCII and fullwidth parentheses (one nesting level is enough for job ads)
_PAREN_RE = re.compile(r"[（(][^()（）]*[)）]")


def strip_parentheticals(value: str) -> tuple[str, list[str]]:
    """Remove every parenthetical chunk from ``value``.

    Returns ``(main_text, removed_chunks)``. A value that is *entirely*
    parenthetical collapses to the empty string (the caller treats that as a
    placeholder).
    """

    removed: list[str] = []
    current = value
    while True:
        match = _PAREN_RE.search(current)
        if match is None:
            break
        removed.append(unify_text(match.group(0).strip("()（）")))
        current = current[: match.start()] + " " + current[match.end() :]
    cleaned = " ".join(current.split())
    if removed and not cleaned:
        # value was entirely parenthetical; keep the innermost content as main
        cleaned = removed[-1]
        removed = removed[:-1]
    return cleaned, [chunk for chunk in removed if chunk]
