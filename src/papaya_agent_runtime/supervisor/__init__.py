"""Local supervisor and per-task runner guardians.

The supervisor is a local daemon reachable over a Unix domain socket. Each task
runs under a runner guardian that owns one worker subprocess, streams its
normalized events to an append-only spool and to SQLite, and records a
fail-closed result. State is durable so a dropped connection or restart is
recoverable without losing work or inferring success from a missing process.
"""

from papaya_agent_runtime.supervisor.core import Supervisor
from papaya_agent_runtime.supervisor.runner import RunnerGuardian

__all__ = ["Supervisor", "RunnerGuardian"]
