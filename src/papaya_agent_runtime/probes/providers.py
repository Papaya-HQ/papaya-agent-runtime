"""Provider-specific command construction and event parsing for probes.

Keeps every provider quirk (flags, event shapes, session-id location, resume
form) isolated here, matching the adapter boundary in the technical plan. The
scenario runner stays provider-neutral and consumes only this interface.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass

from papaya_agent_runtime.probes.process import capture


@dataclass
class ProviderSpec:
    name: str
    version: str
    model: str | None
    base_argv: Callable[[str, str, str | None], list[str]]
    resume_argv: Callable[[str, str, str | None], list[str]]
    extract_session_id: Callable[[list[str]], str | None]
    has_usage: Callable[[list[str]], bool]
    tool_start_predicate: Callable[[str], bool]
    model_turn_predicate: Callable[[str], bool]
    notfound_markers: tuple[str, ...]
    requires_resume_prompt: bool
    tool_prompt: str
    trivial_prompt: str
    resume_prompt: str


def _iter_json(lines: list[str]):
    for line in lines:
        line = line.strip()
        if not line or not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            yield json.loads(line)
        except (ValueError, TypeError):
            continue


def _find_key(obj: object, key: str) -> str | None:
    """Depth-first search for the first string value under ``key``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str) and v:
                return v
            found = _find_key(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found:
                return found
    return None


# --------------------------------------------------------------------------- #
# Claude Code (claude -p, stream-json)
# --------------------------------------------------------------------------- #


def _claude_base(prompt: str, cwd: str, model: str | None) -> list[str]:
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "default",
        "--allowedTools",
        "Bash",
    ]
    if model:
        argv += ["--model", model]
    return argv


def _claude_resume(session_id: str, prompt: str, model: str | None) -> list[str]:
    argv = [
        "claude",
        "-p",
        "--resume",
        session_id,
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if model:
        argv += ["--model", model]
    return argv


def _claude_session_id(lines: list[str]) -> str | None:
    for obj in _iter_json(lines):
        sid = _find_key(obj, "session_id")
        if sid:
            return sid
    return None


def _claude_usage(lines: list[str]) -> bool:
    for obj in _iter_json(lines):
        if _find_key(obj, "usage") is not None or (isinstance(obj, dict) and "usage" in obj):
            return True
    return any('"usage"' in line for line in lines)


def _claude_tool_start(line: str) -> bool:
    return '"tool_use"' in line or '"name":"Bash"' in line


def _claude_model_turn(line: str) -> bool:
    return (
        '"content_block_delta"' in line
        or '"text_delta"' in line
        or ('"type":"assistant"' in line and '"text"' in line)
    )


# --------------------------------------------------------------------------- #
# Codex (codex exec, --json)
# --------------------------------------------------------------------------- #


def _codex_base(prompt: str, cwd: str, model: str | None) -> list[str]:
    argv = [
        "codex",
        "exec",
        prompt,
        "--json",
        "--skip-git-repo-check",
        "-C",
        cwd,
        "-s",
        "workspace-write",
    ]
    if model:
        argv += ["--model", model]
    return argv


def _codex_resume(session_id: str, prompt: str, model: str | None) -> list[str]:
    argv = [
        "codex",
        "exec",
        "resume",
        session_id,
        prompt,
        "--json",
        "--skip-git-repo-check",
    ]
    if model:
        argv += ["--model", model]
    return argv


def _codex_session_id(lines: list[str]) -> str | None:
    for obj in _iter_json(lines):
        for key in ("thread_id", "session_id", "conversation_id"):
            sid = _find_key(obj, key)
            if sid:
                return sid
    return None


def _codex_usage(lines: list[str]) -> bool:
    for obj in _iter_json(lines):
        for key in ("usage", "token_usage", "input_tokens", "total_tokens"):
            if isinstance(obj, dict) and _find_key(obj, key) is not None:
                return True
    return any(("usage" in line or "token" in line) for line in lines)


def _codex_tool_start(line: str) -> bool:
    return (
        "command_execution" in line
        or "CommandExecution" in line
        or "exec_command" in line
        or '"sleep"' in line
        or "item.started" in line
    )


def _codex_model_turn(line: str) -> bool:
    return (
        "turn.started" in line
        or "agent_message" in line
        or ("item.started" in line and "reasoning" in line)
    )


def detect_claude(model: str | None) -> ProviderSpec | None:
    out = capture(["claude", "--version"])
    if not out.strip():
        return None
    version = out.strip().split()[0]
    return ProviderSpec(
        name="claude",
        version=version,
        model=model,
        base_argv=_claude_base,
        resume_argv=_claude_resume,
        extract_session_id=_claude_session_id,
        has_usage=_claude_usage,
        tool_start_predicate=_claude_tool_start,
        model_turn_predicate=_claude_model_turn,
        notfound_markers=(
            "No conversation found",
            "No conversations found",
            "Failed to resume",
        ),
        requires_resume_prompt=False,
        tool_prompt=(
            "Run exactly this bash command and nothing else: `sleep 20`. "
            "After it finishes, reply with the single word DONE."
        ),
        trivial_prompt="Reply with the single word READY. Do not use any tools.",
        resume_prompt="Reply with the single word AGAIN. Do not use any tools.",
    )


def detect_codex(model: str | None) -> ProviderSpec | None:
    out = capture(["codex", "--version"])
    if not out.strip():
        return None
    # e.g. "codex-cli 0.150.1"
    parts = out.strip().split()
    version = parts[-1] if parts else out.strip()
    return ProviderSpec(
        name="codex",
        version=version,
        model=model,
        base_argv=_codex_base,
        resume_argv=_codex_resume,
        extract_session_id=_codex_session_id,
        has_usage=_codex_usage,
        tool_start_predicate=_codex_tool_start,
        model_turn_predicate=_codex_model_turn,
        notfound_markers=(
            "No session",
            "No recorded session",
            "no rollout",
        ),
        # Codex exec resume always constructs an initial user turn; there is no
        # promptless follow mode in the headless CLI.
        requires_resume_prompt=True,
        tool_prompt=(
            "Run exactly this shell command and nothing else: sleep 20. "
            "After it finishes, reply with the single word DONE."
        ),
        trivial_prompt="Reply with the single word READY. Do not run any commands.",
        resume_prompt="Reply with the single word AGAIN. Do not run any commands.",
    )


def default_models() -> dict[str, str | None]:
    """Cheapest eligible model per provider, overridable via env."""
    return {
        "claude": os.environ.get("PPY_CLAUDE_MODEL", "haiku") or None,
        # None => use the Codex-configured default; override with PPY_CODEX_MODEL.
        "codex": os.environ.get("PPY_CODEX_MODEL") or None,
    }
