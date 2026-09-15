"""Plain references: the manager describes things, it does not cite labels.

The user's rule (2026-08-30): when the manager speaks to them, every reference must
be described as what it is — "the plan step that adds per-item summaries and links
(the second backend PR)" — never as a bare internal label the reader would have to
look up: "1B", "§5", "rev3", "informs 1B/1D", "migration order 1C, 1B, 1D". A label
may follow once, in parentheses, for people who want to grep; the sentence has to
stand on its own.

This module is the cheap mechanical half of that rule. It scans a reply for the
label shapes that plans and specs produce — section sigils, revision tags, and
plan-step codes — after removing everything that legitimately carries them (code
spans, fenced blocks, URLs, and parentheticals). The Stop hook uses it to bounce a
reply back to the manager once, with the offending labels named, before the reply
reaches the user. It is a heuristic: it catches the common shapes, and a false
positive costs one rewrite, never a stuck session.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

_FENCED = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+")
_PARENTHETICAL = re.compile(r"\([^()\n]*\)")
_MARKDOWN_LINK_TARGET = re.compile(r"\]\([^)]*\)")

LABEL_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"§\s*\d+(?:\.\d+)*"), "section sigil"),
    (re.compile(r"\brev\s?\d+\b", re.IGNORECASE), "revision tag"),
    (re.compile(r"\b[1-9][A-E][1-9]?\b"), "plan-step code"),
)


def scrub(text: str) -> str:
    """Remove the places a label is allowed to appear verbatim."""
    text = _FENCED.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _MARKDOWN_LINK_TARGET.sub("]", text)
    text = _URL.sub(" ", text)
    text = _PARENTHETICAL.sub(" ", text)
    return text


def findings(text: str) -> list[str]:
    """Bare labels in ``text`` that a reader without the source could not follow.

    Returns the distinct offending tokens in order of first appearance.
    """
    seen: list[str] = []
    scrubbed = scrub(text)
    hits: list[tuple[int, str]] = []
    for pattern, _kind in LABEL_SHAPES:
        hits.extend((m.start(), m.group(0).strip()) for m in pattern.finditer(scrubbed))
    for _pos, token in sorted(hits):
        if token not in seen:
            seen.append(token)
    return seen


def block_reason(tokens: Iterable[str]) -> str | None:
    """The Stop-hook message that sends the reply back for a plain rewrite."""
    tokens = list(tokens)
    if not tokens:
        return None
    listed = ", ".join(tokens[:8]) + (" …" if len(tokens) > 8 else "")
    return (
        f"Your reply refers to things by internal label: {listed}. The user has asked "
        "that every reference be described as what it is, so a reader who has not seen "
        "the plan can follow — e.g. 'the plan step that adds per-item summaries and links "
        "(the second backend PR)', 'the plan's continuity section', 'the latest plan "
        "revision'. A label may follow once, in parentheses. Rewrite those references in "
        "plain terms and reply again."
    )


def last_assistant_text(payload: dict) -> str:
    """The reply the manager is about to send, from the Stop-hook payload.

    Prefers the harness's own ``last_assistant_message`` field; otherwise reads the
    final assistant entry from the JSONL transcript at ``transcript_path``. Returns
    "" when neither is usable — a hook must never break the harness.
    """
    direct = payload.get("last_assistant_message")
    if isinstance(direct, str) and direct.strip():
        return direct
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


def stop_reason_for(payload: dict) -> str | None:
    """Block reason for the Stop hook, or None when the reply reads plainly."""
    return block_reason(findings(last_assistant_text(payload)))
