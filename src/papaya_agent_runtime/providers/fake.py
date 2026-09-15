"""A deterministic fake provider for kernel testing.

It exercises the full runner/spool/supervisor path without any network or model
cost: the worker is a local Python subprocess that emits a normalized JSONL
event stream and makes a real commit in its leased worktree. Instruction markers
drive behavior so tests can cover the blocked/question and failure paths.
"""

from __future__ import annotations

import json
import os
import sys

import papaya_agent_runtime
from papaya_agent_runtime.providers.base import (
    ProviderAdapter,
    ProviderEvent,
    TaskSpec,
    UsageInfo,
    WorkerResult,
)

# papaya_agent_runtime.__file__ -> <src>/papaya_agent_runtime/__init__.py; two dirnames -> <src>.
_SRC_DIR = os.path.dirname(os.path.dirname(papaya_agent_runtime.__file__))


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _SRC_DIR + (os.pathsep + existing if existing else "")
    return env


class FakeProvider(ProviderAdapter):
    name = "fake"

    def probe(self) -> dict:
        # The fake provider claims nothing beyond deterministic completion.
        return {
            "provider": "fake",
            "cli_version": "fake-1",
            "capabilities": {"session_id_in_stream": True, "resume_after_clean_exit": True},
        }

    def _argv(self, spec: TaskSpec) -> list[str]:
        payload = {
            "task_id": spec.task_id,
            "title": spec.title,
            "instructions": spec.instructions,
            "worktree_path": spec.worktree_path,
            "branch": spec.branch,
            "resume_session_id": spec.resume_session_id,
            "steer_message": spec.steer_message,
        }
        return [
            sys.executable,
            "-m",
            "papaya_agent_runtime.providers.fake_worker",
            "--spec",
            json.dumps(payload),
        ]

    def start(self, spec: TaskSpec) -> list[str]:
        return self._argv(spec)

    def resume(self, spec: TaskSpec) -> list[str]:
        return self._argv(spec)

    def reconcile(self, spec: TaskSpec) -> dict:
        return {
            "provider": "fake",
            "resume_session_id": spec.resume_session_id,
            "note": "fake provider is deterministic; restart is safe",
        }

    def parse_event(self, line: str) -> ProviderEvent | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        kind = obj.get("type", "progress")
        return ProviderEvent(
            kind=kind,
            raw=obj,
            session_id=obj.get("session_id"),
            text=obj.get("text"),
        )

    def parse_usage(self, events: list[ProviderEvent]) -> UsageInfo | None:
        for ev in events:
            if ev.kind == "usage" or "usage" in ev.raw:
                u = ev.raw.get("usage", ev.raw)
                return UsageInfo(
                    provider="fake",
                    input_tokens=int(u.get("input_tokens", 0)),
                    output_tokens=int(u.get("output_tokens", 0)),
                )
        return None

    def result(self, events: list[ProviderEvent], exit_code: int | None) -> WorkerResult:
        session_id = None
        usage = self.parse_usage(events)
        result_ev = None
        error_text = None
        for ev in events:
            if ev.session_id:
                session_id = ev.session_id
            if ev.kind == "result":
                result_ev = ev
            if ev.kind == "error" and ev.text:
                error_text = ev.text
        # Fail-closed: a missing result event is a failure, never an assumed success.
        # The worker's own error text carries the reason — the remote it refused to
        # push to, say — so the failure names it instead of only reporting an exit code.
        if result_ev is None:
            reason = error_text or "no result event"
            return WorkerResult(
                status="failed",
                summary=f"{reason} (exit={exit_code})",
                session_id=session_id,
                usage=usage,
            )
        raw = result_ev.raw
        status = raw.get("status", "completed")
        return WorkerResult(
            status=status,
            summary=raw.get("summary", ""),
            session_id=session_id,
            usage=usage,
            question=raw.get("question"),
            head_sha=raw.get("head_sha"),
        )

    def child_env(self) -> dict[str, str]:
        return _child_env()
