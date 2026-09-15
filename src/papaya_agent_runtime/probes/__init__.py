"""Live provider interrupt/resume probes (milestone M0).

The probe harness measures the real interrupt and resume behavior of the
installed Claude Code and Codex CLIs, which the technical plan names as the
largest technical uncertainty. Results are recorded fail-closed: every
capability defaults to false and is set true only by an explicit passing probe.

Nothing here builds the supervisor, Treehouse, setup wizard, or real worker
adapters. It only produces the capability matrix that those later milestones
consume.
"""

from papaya_agent_runtime.probes.capability import (
    CAPABILITY_FIELDS,
    CapabilityRecord,
    ScenarioResult,
)

__all__ = ["CAPABILITY_FIELDS", "CapabilityRecord", "ScenarioResult"]
