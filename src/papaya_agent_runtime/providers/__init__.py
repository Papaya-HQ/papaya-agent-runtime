"""Provider adapters.

Each adapter isolates one provider's quirks behind a single contract
(``probe/start/resume/steer/interrupt/reconcile/parse_event/parse_usage/
result``). The rest of the system speaks one normalized task/event/result/usage
protocol. M2 ships the fake provider; M3 adds Claude and Codex.
"""

from papaya_agent_runtime.providers.base import (
    ProviderAdapter,
    ProviderEvent,
    TaskSpec,
    UsageInfo,
    WorkerResult,
)

__all__ = [
    "ProviderAdapter",
    "ProviderEvent",
    "TaskSpec",
    "UsageInfo",
    "WorkerResult",
]
