"""Render probe results into the human-readable capability matrix."""

from __future__ import annotations

import re
from typing import cast

from papaya_agent_runtime.probes.capability import (
    CAPABILITY_FIELDS,
    Capabilities,
    CapabilityRecord,
    ScenarioResult,
    ScenarioStatus,
)

_FLAG_DOC = {
    "session_id_in_stream": "Durable session/thread id emitted in the event stream",
    "usage_in_stream": "Token/usage accounting in the event stream",
    "resume_after_clean_exit": "Resume by id after a clean completion",
    "session_survives_process_exit": "Session transcript persists after exit",
    "resume_after_sigint_model_turn": "Resume after SIGINT during a model turn",
    "resume_after_sigint_tool": "Resume after SIGINT during a long tool command",
    "resume_after_sigkill": "Resume after the process group was SIGKILLed",
    "worktree_resume": "Resume by id from a linked git worktree",
    "duplicate_resume_ok": "Resuming the same id twice both succeed",
    "mid_process_steer": "Accepts a follow-up into the same running process",
    "requires_resume_prompt": "Resume requires a prompt (no promptless follow)",
    "orphan_tool_process": "A tool child survives a process-group SIGINT",
}


def _mark(value: bool) -> str:
    return "yes" if value else "no"


def render_markdown(records: list[CapabilityRecord]) -> str:
    lines: list[str] = []
    lines.append("# Provider capability matrix")
    lines.append("")
    lines.append(
        "Fail-closed record of the installed Claude/Codex CLIs' interrupt and "
        "resume behavior, produced by the milestone M0 probe harness "
        "(`python -m papaya_agent_runtime.probes`). Every capability defaults to "
        "`no` and is set `yes` only by a passing live probe."
    )
    lines.append("")
    lines.append(
        "Adapters (milestone M3) may claim mid-flight interrupt steering only "
        "when the matching flag is `yes`. Otherwise they must fall back to "
        "checkpoint-at-completion steering plus a fresh-session recovery packet."
    )
    lines.append("")
    lines.append(
        "How to read interrupt timing: `resume_after_sigint_tool` is the "
        "strongest mid-work proof because it interrupts a real long-running tool "
        "command (`sleep 20`, observed `in_progress`). `resume_after_sigkill` "
        "uses an uncatchable signal, so it proves crash recovery. "
        "`resume_after_sigint_model_turn` only proves that a SIGINT delivered "
        "during an active (not yet completed) turn is still resumable; the exact "
        "interrupt instant is racy and per-provider, so read each scenario's "
        "`base exit`/`dur` detail rather than assuming a mid-generation kill."
    )
    lines.append("")

    if not records:
        lines.append("_No providers were detected or probed._")
        lines.append("")
        return "\n".join(lines)

    header = "| Capability | " + " | ".join(f"{r.provider} {r.cli_version}" for r in records) + " |"
    sep = "| --- | " + " | ".join("---" for _ in records) + " |"
    lines.append(header)
    lines.append(sep)
    for flag in CAPABILITY_FIELDS:
        doc = _FLAG_DOC.get(flag, flag)
        cells = " | ".join(_mark(getattr(r.capabilities, flag)) for r in records)
        lines.append(f"| {doc} (`{flag}`) | {cells} |")
    lines.append("")

    for r in records:
        lines.append(f"## {r.provider} {r.cli_version}")
        lines.append("")
        lines.append(f"- Model: `{r.model or 'CLI default'}`")
        lines.append(f"- Probed at: {r.probed_at}")
        lines.append(f"- Probe harness: {r.probe_tool_version}")
        lines.append("")
        lines.append("| Scenario | Status | Detail |")
        lines.append("| --- | --- | --- |")
        for s in r.scenarios:
            detail = s.detail.replace("|", "\\|")
            lines.append(f"| {s.name} | {s.status} | {detail} |")
        lines.append("")

    return "\n".join(lines)


_SCENARIO_ROW = re.compile(r"^\| (\S+) \| (\S+) \| (.*) \|$")


def carried_records(tracked: dict, markdown: str, *, probed: set[str]) -> list[CapabilityRecord]:
    """Rebuild the published records of providers this run did not probe.

    A maintainer who has only one CLI installed still publishes: the other
    provider's row stays exactly as it was last proved, its flags from the tracked
    JSON and its scenario evidence from its section of the previous matrix, rather
    than disappearing from both files.
    """
    out: list[CapabilityRecord] = []
    for provider, row in sorted(tracked.items()):
        if provider in probed:
            continue
        caps = Capabilities()
        for flag, value in (row.get("capabilities") or {}).items():
            if flag in CAPABILITY_FIELDS:
                setattr(caps, flag, bool(value))
        record = CapabilityRecord(
            provider=provider,
            cli_version=str(row.get("cli_version") or "?"),
            probed_at=str(row.get("probed_at") or "?"),
            probe_tool_version="?",
            model=row.get("model"),
            capabilities=caps,
        )
        _read_section(record, markdown)
        out.append(record)
    return out


def _read_section(record: CapabilityRecord, markdown: str) -> None:
    heading = f"## {record.provider} {record.cli_version}"
    inside = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            inside = line.strip() == heading
            continue
        if not inside:
            continue
        if line.startswith("- Probe harness: "):
            record.probe_tool_version = line.removeprefix("- Probe harness: ").strip()
            continue
        m = _SCENARIO_ROW.match(line)
        if m and m.group(1) not in ("Scenario", "---"):
            status = cast(ScenarioStatus, m.group(2))
            record.scenarios.append(
                ScenarioResult(m.group(1), status, m.group(3).replace("\\|", "|"))
            )
