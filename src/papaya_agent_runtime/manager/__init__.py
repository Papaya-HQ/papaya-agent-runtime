"""The manager entry point (`ppy start`, and the turns `ppy serve` runs).

Launches the configured manager harness (Claude or Codex) as an interactive
session with the Papaya Agent Runtime runtime contract in force, so the user talks to
one manager in natural language and the manager drives the whole `ppy` control
plane on their behalf. The same launcher builds a headless turn for `ppy serve`.
"""

from __future__ import annotations

from papaya_agent_runtime.manager.launch import (
    Launch,
    ManagerLaunchError,
    TurnResult,
    build_launch,
    run_turn,
    start,
)

__all__ = ["Launch", "ManagerLaunchError", "TurnResult", "build_launch", "run_turn", "start"]
