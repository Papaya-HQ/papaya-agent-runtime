"""The interactive manager entry point (`ppy start`).

Launches the configured manager harness (Claude or Codex) as an interactive
session with the Papaya Agent Runtime runtime contract in force, so the user talks to
one manager in natural language and the manager drives the whole `ppy` control
plane on their behalf.
"""

from __future__ import annotations

from papaya_agent_runtime.manager.launch import Launch, ManagerLaunchError, build_launch, start

__all__ = ["Launch", "ManagerLaunchError", "build_launch", "start"]
