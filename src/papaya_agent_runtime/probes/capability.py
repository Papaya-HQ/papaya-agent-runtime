"""Fail-closed capability records for provider probes.

Every capability boolean defaults to ``False``. A capability becomes ``True``
only when a scenario explicitly proves it. This mirrors the technical plan:
until a provider/version passes the resume/interruption probes, its capability
record must not claim mid-flight continuation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Literal

# Ordered list of capability flags. Kept in sync with
# schemas/provider-capability.schema.json (see tests/test_capability.py).
CAPABILITY_FIELDS: tuple[str, ...] = (
    "session_id_in_stream",
    "usage_in_stream",
    "resume_after_clean_exit",
    "session_survives_process_exit",
    "resume_after_sigint_model_turn",
    "resume_after_sigint_tool",
    "resume_after_sigkill",
    "worktree_resume",
    "duplicate_resume_ok",
    "mid_process_steer",
    "requires_resume_prompt",
    "orphan_tool_process",
)

ScenarioStatus = Literal["proved", "disproved", "inconclusive", "error", "skipped"]


@dataclass
class Capabilities:
    """Derived provider capabilities. All default to False (fail-closed)."""

    session_id_in_stream: bool = False
    usage_in_stream: bool = False
    resume_after_clean_exit: bool = False
    session_survives_process_exit: bool = False
    resume_after_sigint_model_turn: bool = False
    resume_after_sigint_tool: bool = False
    resume_after_sigkill: bool = False
    worktree_resume: bool = False
    duplicate_resume_ok: bool = False
    mid_process_steer: bool = False
    requires_resume_prompt: bool = False
    orphan_tool_process: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class ScenarioResult:
    """Evidence from a single probe scenario."""

    name: str
    status: ScenarioStatus
    detail: str = ""
    session_id: str | None = None
    duration_s: float | None = None
    evidence_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class CapabilityRecord:
    """A provider/version capability record, ready to serialize."""

    provider: str
    cli_version: str
    probed_at: str
    probe_tool_version: str
    model: str | None = None
    capabilities: Capabilities = field(default_factory=Capabilities)
    scenarios: list[ScenarioResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "cli_version": self.cli_version,
            "model": self.model,
            "probed_at": self.probed_at,
            "probe_tool_version": self.probe_tool_version,
            "capabilities": self.capabilities.to_dict(),
            "scenarios": [s.to_dict() for s in self.scenarios],
        }
