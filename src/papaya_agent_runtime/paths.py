"""Filesystem layout for Papaya Agent Runtime runtime state.

Canonical state lives under a gitignored ``.ppy/`` directory (see the technical
plan). The location is the current working directory by default, overridable
with the ``PPY_HOME`` environment variable. The ``bin/ppy`` launcher always sets
``PPY_HOME`` to its own checkout's ``.ppy/`` (unless already set), so the control
plane addresses the same instance no matter which directory it is invoked from;
the cwd fallback exists for tests and for running the module directly.
"""

from __future__ import annotations

import os
from pathlib import Path


def ppy_home() -> Path:
    """Return the ``.ppy`` root, honoring the ``PPY_HOME`` override."""
    override = os.environ.get("PPY_HOME")
    if override:
        return Path(override)
    return Path.cwd() / ".ppy"


def config_path() -> Path:
    return ppy_home() / "config.toml"


def db_path() -> Path:
    return ppy_home() / "state.db"


def repos_dir() -> Path:
    return ppy_home() / "repos"


def runs_dir() -> Path:
    return ppy_home() / "runs"


def run_dir() -> Path:
    return ppy_home() / "run"


def tools_dir() -> Path:
    return ppy_home() / "tools"


def tools_bin_dir() -> Path:
    """Aggregated symlink dir for provisioned companion executables."""
    return tools_dir() / "bin"


def worktree_pools_dir() -> Path:
    return ppy_home() / "worktree-pools"


def treehouse_home() -> Path:
    """Where the treehouse companion keeps its worktree pools.

    Treehouse owns this directory, not us, so it is not under ``.ppy``. The
    overrides exist so the hermetic suite (and anyone running two instances) can
    point the pool scan at a scratch directory instead of the real pool.
    """
    override = os.environ.get("PPY_TREEHOUSE_HOME") or os.environ.get("TREEHOUSE_HOME")
    return Path(override) if override else Path.home() / ".treehouse"


def cache_dir() -> Path:
    """Caches worker processes share. Under ``.ppy``, so it is writable from a sandbox."""
    return ppy_home() / "cache"


def uv_cache_dir() -> Path:
    """Where ``uv`` downloads and unpacks. Exported to every worker as ``UV_CACHE_DIR``."""
    return cache_dir() / "uv"


def memory_dir() -> Path:
    return ppy_home() / "memory"


def local_capabilities_path() -> Path:
    """Machine-local provider capability record, written by the live probe.

    The tracked ``provider-capabilities.json`` at the repo root describes
    whatever CLI versions the maintainer probed. This file describes *this*
    machine, takes precedence per provider, and is gitignored with the rest of
    ``.ppy`` so a local probe never shows up as a repo diff.
    """
    return ppy_home() / "provider-capabilities.json"


def ensure_layout() -> Path:
    """Create the ``.ppy`` directory tree if missing and return the root."""
    root = ppy_home()
    for path in (
        root,
        repos_dir(),
        runs_dir(),
        run_dir(),
        tools_dir(),
        tools_bin_dir(),
        worktree_pools_dir(),
        cache_dir(),
        uv_cache_dir(),
        memory_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
    return root
