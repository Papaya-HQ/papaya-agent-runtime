"""Words for a person, not a harness's tool-call markup.

A harness turn's text is forwarded into Papaya chat and onto work items. Sometimes
the model wrote a tool call as text (`<function_calls>`, `<tool_call>`, a JSON call
object) instead of making it, and that markup would read as broken output to the
person. :func:`clean_outbound` removes it and leaves everything else alone: prose
around it, fenced code blocks, inline code, and XML or JSON quoted on purpose.

Pure: no I/O, no clock. Applied once, in ``papaya_events._papaya_request``.
"""

from __future__ import annotations

import json
import re

#: What a person reads when nothing but markup was left: honest, never empty.
FALLBACK_LINE = "I could not finish this one; nothing to show yet."

# Placeholders for code held out of the scrub; private-use characters no text carries.
_OPEN, _CLOSE = "", ""
_PLACEHOLDER = re.compile(f"{_OPEN}(\\d+){_CLOSE}")

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(?P<t>`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)(?P=t)(?!`)", re.DOTALL)

#: The tag names a harness writes a call or its result with (Anthropic's, with or
#: without the `antml:` namespace, and the common `<tool_call>` variants). Any other
#: tag, `<br>` included, is prose.
_TAGS = r"(?:antml:)?(?:function_calls|invoke|tool_calls?|tool_use|function_results|tool_results?)"
_BLOCK = re.compile(rf"<(?P<tag>{_TAGS})\b[^>]*>.*?</(?P=tag)\s*>", re.DOTALL | re.IGNORECASE)
_OPENING = re.compile(rf"<(?P<tag>{_TAGS})\b[^>]*>", re.IGNORECASE)
_CLOSING = re.compile(rf"</{_TAGS}\s*>", re.IGNORECASE)
#: What follows an opening tag that was never closed when it is a call cut short: a
#: child tag, a JSON object, or the start of a `name=`/`"name"` field.
_CALL_SHAPE = re.compile(r"\s*(?:<[a-z/]|\{|\[|name\s*=|\"name\")", re.IGNORECASE)

_JSON_START = re.compile(r"^[ \t]*\{", re.MULTILINE)
_TRUNCATED_CALL = re.compile(
    r"^[ \t]*\{\s*\"name\"\s*:\s*\"[^\"]*\"\s*,\s*\"(?:arguments|input)\"\s*:", re.MULTILINE
)
_JSON = json.JSONDecoder()


def _hold_code(text: str) -> tuple[str, list[str]]:
    """``text`` with each fenced block and inline code span swapped for a placeholder."""
    held: list[str] = []

    def hold(code: str) -> str:
        # The line ending stays outside, so trimming the prose around a block can reach it.
        body = code.rstrip("\r\n")
        held.append(body)
        return f"{_OPEN}{len(held) - 1}{_CLOSE}{code[len(body) :]}"

    pieces: list[str] = []
    prose: list[str] = []
    fence: tuple[str, int] | None = None
    block: list[str] = []
    for line in text.splitlines(keepends=True):
        if fence is None:
            opened = _FENCE_OPEN.match(line)
            if opened:
                mark = opened.group(1)
                fence = (mark[0], len(mark))
                block = [line]
            else:
                prose.append(line)
            continue
        block.append(line)
        closing = line.strip()
        if closing and closing == fence[0] * len(closing) and len(closing) >= fence[1]:
            if prose:
                pieces.append("".join(prose))
                prose = []
            pieces.append(hold("".join(block)))
            fence = None
    if fence is not None:  # an unterminated fence runs to the end
        if prose:
            pieces.append("".join(prose))
            prose = []
        pieces.append(hold("".join(block)))
    if prose:
        pieces.append("".join(prose))
    # Inline code is looked for only in what is not already a held fence.
    masked = "".join(
        piece if _PLACEHOLDER.match(piece) else _INLINE_CODE.sub(lambda m: hold(m.group(0)), piece)
        for piece in pieces
    )
    return masked, held


def _drop_unclosed(text: str) -> str:
    """An opening tag with no close: a cut-short call goes to the end, a bare tag alone."""
    while True:
        found = _OPENING.search(text)
        if found is None:
            return text
        if _CALL_SHAPE.match(text, found.end()):
            return text[: found.start()]
        text = text[: found.start()] + text[found.end() :]


def _drop_json_calls(text: str) -> str:
    """Remove JSON tool-call objects: `name` plus `arguments` or `input`, nothing less."""
    out: list[str] = []
    at = 0
    for start in _JSON_START.finditer(text):
        begin = start.end() - 1
        if begin < at:
            continue
        try:
            value, end = _JSON.raw_decode(text, begin)
        except ValueError:
            truncated = _TRUNCATED_CALL.match(text, start.start())
            if truncated:
                out.append(text[at : start.start()])
                return "".join(out)
            continue
        if (
            isinstance(value, dict)
            and "name" in value
            and ("arguments" in value or "input" in value)
        ):
            out.append(text[at : start.start()])
            at = end
    out.append(text[at:])
    return "".join(out)


def clean_outbound(text: str) -> str:
    """``text`` without tool-call markup, or the fallback line if that leaves nothing.

    Text with no markup comes back exactly as given. Empty input stays empty: what
    to do about no text at all is the caller's rule, not this one's.
    """
    if not text or not text.strip():
        return text
    masked, held = _hold_code(text.replace(_OPEN, "").replace(_CLOSE, ""))
    scrubbed = _BLOCK.sub("", masked)
    scrubbed = _drop_unclosed(scrubbed)
    scrubbed = _CLOSING.sub("", scrubbed)
    scrubbed = _drop_json_calls(scrubbed)
    if scrubbed == masked:
        return text
    scrubbed = re.sub(r"[ \t]+\n", "\n", scrubbed)
    scrubbed = re.sub(r"\n{3,}", "\n\n", scrubbed).strip()
    restored = _PLACEHOLDER.sub(lambda m: held[int(m.group(1))], scrubbed)
    return restored if restored.strip() else FALLBACK_LINE
