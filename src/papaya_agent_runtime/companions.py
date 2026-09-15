"""Resolving provisioned companion executables.

Companions (treehouse, lavish-axi, gh-axi) are provisioned by ``ppy setup`` /
``ppy tools install`` into ``.ppy/tools/`` with a stable symlink in
``.ppy/tools/bin/``. Runtime code resolves a companion by preferring the
provisioned copy and falling back to whatever is on ``PATH`` — so Papaya Agent Runtime
works whether the user let us manage the tool or installed it themselves.
"""

from __future__ import annotations

import os
import shutil

from papaya_agent_runtime.paths import tools_bin_dir

COMPANION_NAMES = ("treehouse", "lavish-axi", "gh-axi")


def companion_bin(name: str) -> str | None:
    """Absolute path to a companion executable, or ``None`` if unavailable.

    Resolution order: the provisioned symlink under ``.ppy/tools/bin`` first, then
    the ambient ``PATH``.
    """
    managed = tools_bin_dir() / name
    if managed.exists() and os.access(managed, os.X_OK):
        return str(managed)
    return shutil.which(name)


def has_companion(name: str) -> bool:
    return companion_bin(name) is not None


def pr_tool() -> str | None:
    """The GitHub CLI to shell out to for pull-request work, or ``None``.

    ``gh-axi`` is the provisioned wrapper and is preferred when present; plain
    ``gh`` is the fallback. Callers that only read (the heartbeat's PR/CI lookup)
    and callers that write (delivery's PR creation) resolve the same binary, so a
    machine that can open a PR can also report on it.
    """
    for tool in ("gh-axi", "gh"):
        resolved = companion_bin(tool)
        if resolved:
            return resolved
    return None
