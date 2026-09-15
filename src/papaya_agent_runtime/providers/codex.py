"""Codex worker adapter (`codex exec`, --json).

Codex exec is single-shot: there is no mid-run stdin and resume always starts a
new turn with a prompt. The committed capability matrix reflects this
(``mid_process_steer`` false, ``requires_resume_prompt`` true), so steering is
always checkpoint-at-completion via resume.
"""

from __future__ import annotations

import json

from papaya_agent_runtime.providers.base import (
    ProviderAdapter,
    ProviderEvent,
    TaskSpec,
    UsageInfo,
    WorkerResult,
)
from papaya_agent_runtime.providers.capability import allows_interrupt_steer, capabilities_for
from papaya_agent_runtime.providers.command_rules import command_rules


class CodexAdapter(ProviderAdapter):
    name = "codex"

    def probe(self) -> dict:
        return capabilities_for("codex")

    def worker_prompt(self, spec: TaskSpec) -> str:
        """No command rules (Codex has a real shell), but the same environment block.

        Where receipts go, which suite is the local gate, and which database stack
        is this task's are facts about the repository, so a Codex worker reads the
        block a Claude worker does — from the same function.
        """
        block = command_rules(self.name, spec.branch, environment=spec.environment)
        body = super().worker_prompt(spec)
        return f"{block}\n---\n\n{body}" if block else body

    def start(self, spec: TaskSpec) -> list[str]:
        argv = [
            "codex",
            "exec",
            self.worker_prompt(spec),
            "--json",
            "--skip-git-repo-check",
            "-C",
            spec.worktree_path,
            "-s",
            "workspace-write",
        ]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.reasoning:
            argv += ["-c", f'model_reasoning_effort="{spec.reasoning}"']
        return argv

    def resume(self, spec: TaskSpec) -> list[str]:
        prompt = spec.steer_message or spec.instructions or "Continue the task."
        argv = [
            "codex",
            "exec",
            "resume",
            spec.resume_session_id or "",
            prompt,
            "--json",
            "--skip-git-repo-check",
        ]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.reasoning:
            argv += ["-c", f'model_reasoning_effort="{spec.reasoning}"']
        return argv

    def supports_interrupt_steer(self) -> bool:
        # Even if resume-after-interrupt is proven, exec cannot accept mid-run
        # input, so mid-flight steering is never offered for Codex.
        return allows_interrupt_steer("codex") and capabilities_for("codex")["mid_process_steer"]

    def reconcile(self, spec: TaskSpec) -> dict:
        caps = capabilities_for("codex")
        return {
            "provider": "codex",
            "resume_session_id": spec.resume_session_id,
            "resumable": caps["resume_after_sigkill"],
            "requires_prompt": caps["requires_resume_prompt"],
            "note": "resume by thread id with a fresh prompt",
        }

    def parse_event(self, line: str) -> ProviderEvent | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        session_id = None
        for key in ("thread_id", "session_id", "conversation_id"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                session_id = val
                break
        text = None
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
        return ProviderEvent(
            kind=obj.get("type", "progress"), raw=obj, session_id=session_id, text=text
        )

    def parse_usage(self, events: list[ProviderEvent]) -> UsageInfo | None:
        for ev in reversed(events):
            usage = ev.raw.get("usage")
            if isinstance(usage, dict):
                return UsageInfo(
                    provider="codex",
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                )
        return None

    def result(self, events: list[ProviderEvent], exit_code: int | None) -> WorkerResult:
        session_id = None
        last_message = None
        completed = False
        for ev in events:
            if ev.session_id:
                session_id = ev.session_id
            if ev.text:
                last_message = ev.text
            if ev.raw.get("type") == "turn.completed":
                completed = True
        usage = self.parse_usage(events)
        # Fail-closed: require an explicit completion signal and a clean exit.
        if not completed or (exit_code not in (0, None)):
            return WorkerResult(
                status="failed",
                summary=f"no clean completion (exit={exit_code})",
                session_id=session_id,
                usage=usage,
            )
        return WorkerResult(
            status="completed",
            summary=(last_message or "")[:500],
            session_id=session_id,
            usage=usage,
        )
