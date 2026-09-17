"""Claude Code worker adapter (`claude -p`, stream-json).

Interrupt steering is only offered when the committed capability matrix proves it
for the installed version (see providers/capability.py); otherwise the base class
falls back to checkpoint-at-completion steering via resume.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from papaya_agent_runtime.config import (
    ConfigError,
    default_claude_allowed_tools,
    effective_claude_tools,
    load_config,
)
from papaya_agent_runtime.providers.base import (
    ProviderAdapter,
    ProviderEvent,
    TaskSpec,
    UsageInfo,
    WorkerResult,
)
from papaya_agent_runtime.providers.capability import (
    allows_interrupt_steer,
    capabilities_for,
)
from papaya_agent_runtime.providers.command_rules import command_rules

ALLOWED_TOOLS_ENV = "PPY_CLAUDE_ALLOWED_TOOLS"


def _transcript_path(worktree: str, session_id: str) -> Path:
    """Where Claude Code keeps a session's transcript for a working directory.

    The directory name is the absolute cwd with every character that is not a
    letter or a digit replaced by a dash, which is how the harness itself writes
    it (`~/.claude/projects/-Users-me-workspace-repo/<session>.jsonl`).
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(worktree).resolve()))
    return Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"


def _ends_on_an_assistant_turn(spec: TaskSpec) -> bool:
    """Does this session's transcript end mid-answer, with nothing to reply to?

    False whenever the transcript cannot be read: an unreadable file is not
    evidence of anything, and resuming is still the right default.
    """
    path = _transcript_path(spec.worktree_path, str(spec.resume_session_id))
    try:
        lines = [line for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("type")
        if kind in ("assistant", "user"):
            return kind == "assistant"
    return False


def _fresh_session_prompt(worker_prompt: str, spec: TaskSpec, steer: str) -> str:
    """The brief again, plus what the worker already said and what to do now.

    Everything the retired session held that is worth keeping is already in the
    progress log it filed, so a fresh session reads it rather than starting blind.
    """
    from papaya_agent_runtime.state import init_db

    reported: list[str] = []
    try:
        conn = init_db()
        try:
            rows = conn.execute(
                "SELECT payload FROM events WHERE task_id = ? AND kind = 'worker_progress' "
                "ORDER BY id",
                (spec.task_id,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"] or "{}")
                phase = str(payload.get("phase") or "").strip()
                note = str(payload.get("note") or "").strip()
                if note:
                    reported.append(f"- **{phase or 'note'}**: {note}")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the prompt is worth having without the log
        reported = []
    log = "\n".join(reported) or "- (you filed no progress notes)"
    return (
        f"{worker_prompt}\n\n---\n\n"
        "## You are continuing an interrupted session\n\n"
        "Your previous session could not be resumed, so this is a new one on the same "
        "task, in the same worktree, on the same branch. Your work on disk is intact — "
        "read it before assuming anything is missing. What you reported so far:\n\n"
        f"{log}\n\n"
        f"What you were asked to do next:\n\n{steer}\n"
    )


def effective_allowed_tools() -> tuple[list[str], str]:
    """The tool patterns a Claude worker will be launched with, and where they came from.

    ``PPY_CLAUDE_ALLOWED_TOOLS`` (a comma-separated list) wins when it is set — the
    escape hatch for a one-off session — otherwise the code's profile with the
    config's ``claude.extra_tools`` added and ``claude.dropped_tools`` removed. Read
    on every call, so a tool the runtime learned applies to the very next dispatch.
    Without a config file yet, the profile applies as it is: a worker with no shell
    is never the answer.

    An empty result is a real answer, not a fallback: a deliberately emptied env var
    or a profile with everything dropped means "no tools", and dispatch refuses rather
    than quietly launching a worker that cannot run a command.
    """
    raw = os.environ.get(ALLOWED_TOOLS_ENV)
    if raw is not None:
        return [part.strip() for part in raw.split(",") if part.strip()], ALLOWED_TOOLS_ENV
    try:
        cfg = load_config()
    except ConfigError:
        return list(default_claude_allowed_tools()), "built-in default profile"
    if cfg.claude.allowed_tools is not None:
        return effective_claude_tools(cfg), "config claude.allowed_tools (locked)"
    return effective_claude_tools(cfg), "built-in profile + config deltas"


class ClaudeAdapter(ProviderAdapter):
    name = "claude"

    def probe(self) -> dict:
        return capabilities_for("claude")

    def worker_prompt(self, spec: TaskSpec) -> str:
        """The command rules come first, then the memory preamble and the brief.

        Briefs used to repeat these rules by hand. They are the environment's
        rules, not the task's, so the runtime states them — once, identically,
        every time.
        """
        rules = command_rules(self.name, spec.branch, environment=spec.environment)
        return f"{rules}\n---\n\n{super().worker_prompt(spec)}"

    def _common(self, argv: list[str], spec: TaskSpec) -> list[str]:
        if spec.model:
            argv += ["--model", spec.model]
        # acceptEdits auto-accepts file edits without a full permission bypass.
        argv += ["--permission-mode", "acceptEdits"]
        allowed, _source = effective_allowed_tools()
        allowed = [*allowed, *(t for t in spec.granted_tools if t not in allowed)]
        if allowed:
            argv += ["--allowedTools", ",".join(allowed)]
        denied = list(spec.denied_tools)
        for directory in spec.read_only_dirs:
            # Claude Code confines a session to its working directory, so a brief
            # naming another registered repository as a reference is unreadable
            # without this (2026-09-17, task 30). Read-only is the whole point: the
            # edit tools are refused there, so the reference stays a reference.
            argv += ["--add-dir", directory]
            denied += [
                f"Edit({directory}/**)",
                f"Write({directory}/**)",
                f"NotebookEdit({directory}/**)",
            ]
        if denied:
            argv += ["--disallowedTools", ",".join(denied)]
        return argv

    def start(self, spec: TaskSpec) -> list[str]:
        argv = [
            "claude",
            "-p",
            self.worker_prompt(spec),
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        return self._common(argv, spec)

    def resume(self, spec: TaskSpec) -> list[str]:
        prompt = spec.steer_message or spec.instructions or "Continue the task."
        if spec.resume_session_id and _ends_on_an_assistant_turn(spec):
            # A transcript whose last entry is the assistant's cannot be resumed:
            # `--resume` replays it as a prefilled assistant message and the API
            # answers `400 This model does not support assistant message prefill`,
            # which fails the task instead of continuing it (2026-09-17, task 30,
            # after a steer resume). A fresh session carrying the brief, what the
            # worker already reported, and the steer continues the work instead.
            argv = [
                "claude",
                "-p",
                _fresh_session_prompt(self.worker_prompt(spec), spec, prompt),
                "--output-format",
                "stream-json",
                "--verbose",
            ]
            return self._common(argv, spec)
        argv = [
            "claude",
            "-p",
            "--resume",
            spec.resume_session_id or "",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        return self._common(argv, spec)

    def supports_interrupt_steer(self) -> bool:
        return allows_interrupt_steer("claude")

    def reconcile(self, spec: TaskSpec) -> dict:
        caps = capabilities_for("claude")
        return {
            "provider": "claude",
            "resume_session_id": spec.resume_session_id,
            "resumable": caps["resume_after_sigkill"],
            "note": "resume by session id from the leased worktree",
        }

    def parse_event(self, line: str) -> ProviderEvent | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        return ProviderEvent(
            kind=obj.get("type", "progress"),
            raw=obj,
            session_id=_find(obj, "session_id"),
            text=obj.get("result") if obj.get("type") == "result" else None,
        )

    def permission_denials(self, events: list[ProviderEvent]) -> list[dict]:
        """The tool calls Claude Code refused, from the turn's ``result`` event."""
        denials: list[dict] = []
        for ev in events:
            if ev.raw.get("type") == "result":
                found = ev.raw.get("permission_denials") or []
                denials.extend(d for d in found if isinstance(d, dict))
        return denials

    def live_denial(self, event: ProviderEvent, events: list[ProviderEvent]) -> dict | None:
        """A ``system``/``permission_denied`` line, with the command its ``tool_use`` ran."""
        raw = event.raw
        if raw.get("type") != "system" or raw.get("subtype") != "permission_denied":
            return None
        tool_use_id = raw.get("tool_use_id")
        tool_input: dict = {}
        for earlier in reversed(events):
            # Only an assistant line's message is an object: the permission_denied line
            # itself carries the harness's refusal text as a string `message`.
            message = earlier.raw.get("message")
            if earlier.raw.get("type") != "assistant" or not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            use = next(
                (
                    block
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("id") == tool_use_id
                ),
                None,
            )
            if use is not None:
                tool_input = use.get("input") if isinstance(use.get("input"), dict) else {}
                break
        return {
            "tool_name": raw.get("tool_name"),
            "tool_use_id": tool_use_id,
            "tool_input": tool_input,
        }

    def parse_usage(self, events: list[ProviderEvent]) -> UsageInfo | None:
        for ev in reversed(events):
            usage = _find_dict(ev.raw, "usage")
            if usage:
                return UsageInfo(
                    provider="claude",
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                )
        return None

    def result(self, events: list[ProviderEvent], exit_code: int | None) -> WorkerResult:
        session_id = None
        result_ev = None
        for ev in events:
            if ev.session_id:
                session_id = ev.session_id
            if ev.raw.get("type") == "result":
                result_ev = ev
        usage = self.parse_usage(events)
        # Fail-closed: no result event or a nonzero exit is not a success.
        if result_ev is None:
            return WorkerResult(
                status="failed",
                summary=f"no result event (exit={exit_code})",
                session_id=session_id,
                usage=usage,
            )
        raw = result_ev.raw
        if raw.get("is_error") or raw.get("subtype") not in (None, "success"):
            return WorkerResult(
                status="failed",
                summary=str(raw.get("result", "claude reported an error"))[:500],
                session_id=session_id,
                usage=usage,
            )
        return WorkerResult(
            status="completed",
            summary=str(raw.get("result", ""))[:500],
            session_id=session_id,
            usage=usage,
        )


def _find(obj: object, key: str) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str) and v:
                return v
            found = _find(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find(item, key)
            if found:
                return found
    return None


def _find_dict(obj: object, key: str) -> dict | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, dict):
                return v
            found = _find_dict(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_dict(item, key)
            if found:
                return found
    return None
