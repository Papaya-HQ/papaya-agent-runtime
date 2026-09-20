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


# ── who refused a tool call ─────────────────────────────────────────────────
#
# Claude Code refuses a Bash call in two shapes, and the runtime used to see only
# one of them. Read off the live state database on 2026-09-20, every recorded
# denial since 2026-09-18 (107) fell into exactly these two:
#
# 1. The HARNESS refused — profile, safety check, command shape, working
#    directory. A `{"type":"system","subtype":"permission_denied"}` line names the
#    `tool_use_id` and carries the harness's own words in `message`, often with a
#    `decision_reason_type` (`other`, `subcommandResults`, `safetyCheck`,
#    `asyncAgent`) and a `decision_reason`.
#
# 2. A repository's own **PreToolUse hook** refused. There is NO such line
#    anywhere in the turn. The call appears only in the result's
#    `permission_denials` and in a `user`/`tool_result` with `is_error: true` and
#    `tool_result_meta[].non_execution_kind == "permission-rule"`, whose content is
#    the hook's stderr. All six such denials on record are the prescribed
#    `git push origin HEAD:ppy/task-<n>-<id>`, refused by
#    `.claude/hooks/verify-before-push.sh` in the two monorepos (issues #83, #116).
#
# The `PreToolUse:Bash hook error:` prefix some of those carry is NOT the test —
# two of the six (tasks 58 and 129, older Claude Code) carry the hook's stderr
# with no prefix at all. The absence of the harness's own line is the test.

#: What the harness says when it blocked a path rather than a program. A hook never
#: says this, so it keeps a working-directory refusal from reading as a hook block
#: if the harness ever stops emitting its own line for one.
_HARNESS_WORDS = (
    "was blocked. For security, Claude Code",
    "requires approval",
    "haven't granted it yet",
)


def _tool_result_error(tool_use_id: str, events: list[ProviderEvent]) -> str:
    """The error text the call came back with, or "" — for a hook, its own stderr."""
    for ev in events:
        if ev.raw.get("type") != "user":
            continue
        meta = ev.raw.get("tool_result_meta")
        if isinstance(meta, list) and not any(
            isinstance(m, dict) and m.get("id") == tool_use_id for m in meta
        ):
            continue
        message = ev.raw.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_use_id
                and block.get("is_error")
            ):
                text = block.get("content")
                return text if isinstance(text, str) else ""
    return ""


def refusal_evidence(tool_use_id: str, events: list[ProviderEvent]) -> dict:
    """What this turn's transcript proves about who refused ``tool_use_id``.

    Shaped by :data:`papaya_agent_runtime.providers.base.REFUSAL_FIELDS`. Without a
    tool call id nothing can be attributed, and ``harness_line`` is left True so the
    judgement falls back to the command alone rather than inventing a hook.
    """
    if not tool_use_id:
        return {
            "harness_line": True,
            "decision_reason_type": "",
            "decision_reason": "",
            "message": "",
            "tool_result": "",
        }
    for ev in events:
        raw = ev.raw
        if (
            raw.get("type") == "system"
            and raw.get("subtype") == "permission_denied"
            and raw.get("tool_use_id") == tool_use_id
        ):
            return {
                "harness_line": True,
                "decision_reason_type": raw.get("decision_reason_type") or "",
                "decision_reason": raw.get("decision_reason") or "",
                "message": raw.get("message") if isinstance(raw.get("message"), str) else "",
                "tool_result": "",
            }
    result = _tool_result_error(tool_use_id, events)
    # No line AND no error result is not evidence of a hook — it is no evidence at
    # all, which is the ordinary case for a denial read back from a `result` event
    # with none of the turn's stream beside it. Claiming a hook there would put a
    # false diagnosis on every one of them, which is the defect this whole change
    # exists to stop. Only the hook's own words make it a hook.
    hook_said = bool(result) and not any(word in result for word in _HARNESS_WORDS)
    return {
        "harness_line": not hook_said,
        "decision_reason_type": "",
        "decision_reason": "",
        "message": "",
        "tool_result": result,
    }


#: Where a repository registers hooks for Claude Code, most specific last.
HOOK_SETTINGS_FILES = (
    ".claude/settings.json",
    ".claude/settings.local.json",
)


def registered_hooks(worktree: str | None, tool: str) -> list[dict[str, str]]:
    """The repository's own ``PreToolUse`` hooks that match ``tool``. Never raises.

    Each is ``{"settings": <repo-relative settings file>, "command": <as written>,
    "script": <repo-relative script, when the command names one>}``. An empty list
    means the repository registers none, or that none could be read — which is why
    a diagnosis resting on this alone is recorded as inferred.
    """
    found: list[dict[str, str]] = []
    if not worktree:
        return found
    for name in HOOK_SETTINGS_FILES:
        try:
            raw = (Path(worktree) / name).read_text()
        except OSError:
            continue
        try:
            settings = json.loads(raw)
        except ValueError:
            continue
        hooks = settings.get("hooks") if isinstance(settings, dict) else None
        entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not _matches(entry.get("matcher"), tool):
                continue
            for hook in entry.get("hooks") if isinstance(entry.get("hooks"), list) else []:
                if not isinstance(hook, dict):
                    continue
                command = str(hook.get("command") or "").strip()
                if command:
                    found.append({"settings": name, "command": command, "script": _script(command)})
    return found


def _matches(matcher: object, tool: str) -> bool:
    """Claude Code's matcher: a tool name, a `|` list, `*`, or absent for every tool."""
    if matcher is None or matcher == "" or matcher == "*":
        return True
    if not isinstance(matcher, str):
        return False
    return any(part.strip() == tool for part in matcher.split("|"))


def _script(command: str) -> str:
    """The repo-relative script a hook command runs, or "" when it is not one.

    Claude Code writes the project root as ``${CLAUDE_PROJECT_DIR}``, so a hook of
    this repository is exactly the one named relative to it.
    """
    first = command.split()[0] if command.split() else ""
    for prefix in ("${CLAUDE_PROJECT_DIR}/", "$CLAUDE_PROJECT_DIR/", "./"):
        if first.startswith(prefix):
            return first[len(prefix) :]
    return "" if first.startswith("/") else first


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
        rules = command_rules(
            self.name,
            spec.branch,
            environment=spec.environment,
            runtime_pushes=spec.runtime_pushes,
        )
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
        """The tool calls Claude Code refused, from the turn's ``result`` event.

        Each one is given the ``refusal`` block the rest of the transcript proves
        about it, which is the only place a hook block and a permission refusal
        differ (:func:`refusal_evidence`).
        """
        denials: list[dict] = []
        for ev in events:
            if ev.raw.get("type") == "result":
                found = ev.raw.get("permission_denials") or []
                denials.extend(dict(d) for d in found if isinstance(d, dict))
        for denial in denials:
            denial["refusal"] = refusal_evidence(str(denial.get("tool_use_id") or ""), events)
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
            # This line IS the harness announcing the refusal, so it settles the
            # question the end-of-turn path has to go looking for.
            "refusal": {
                "harness_line": True,
                "decision_reason_type": raw.get("decision_reason_type") or "",
                "decision_reason": raw.get("decision_reason") or "",
                "message": raw.get("message") if isinstance(raw.get("message"), str) else "",
                "tool_result": "",
            },
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
