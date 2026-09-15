"""Hermetic tests for companion resolution and managed provisioning.

The network/npm/tar primitives in ``setup.provision`` are injectable, so these
tests exercise the real orchestration (checksum gate, symlink activation, failure
handling) without touching the network.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from papaya_agent_runtime import companions
from papaya_agent_runtime.paths import tools_bin_dir
from papaya_agent_runtime.setup import provision


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    home = tmp_path / ".ppy"
    monkeypatch.setenv("PPY_HOME", str(home))
    return home


def _fake_lock() -> dict:
    return {
        "tool": {
            "treehouse": {
                "version": "v9.9.9",
                "source": "https://example.invalid/treehouse",
                "sha256_darwin_arm64": "DEADBEEF",
                "sha256_darwin_amd64": "DEADBEEF",
                "sha256_linux_amd64": "DEADBEEF",
                "sha256_linux_arm64": "DEADBEEF",
            },
            "lavish-axi": {"version": "0.0.1", "source": "npm:lavish-axi"},
            "gh-axi": {"version": "0.0.1", "source": "npm:gh-axi"},
        }
    }


def _write_treehouse_archive(dest: Path) -> None:
    """Create a tar.gz containing an executable named ``treehouse``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    binary = b"#!/bin/sh\necho treehouse\n"
    with tarfile.open(dest, "w:gz") as tf:
        info = tarfile.TarInfo("treehouse")
        info.size = len(binary)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(binary))


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #


def test_companion_bin_prefers_managed(ppy_home, monkeypatch) -> None:
    bindir = tools_bin_dir()
    bindir.mkdir(parents=True, exist_ok=True)
    managed = bindir / "treehouse"
    managed.write_text("#!/bin/sh\n")
    managed.chmod(managed.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(companions.shutil, "which", lambda name: "/usr/bin/treehouse")
    assert companions.companion_bin("treehouse") == str(managed)


def test_companion_bin_falls_back_to_path(ppy_home, monkeypatch) -> None:
    monkeypatch.setattr(companions.shutil, "which", lambda name: "/usr/bin/gh-axi")
    assert companions.companion_bin("gh-axi") == "/usr/bin/gh-axi"
    monkeypatch.setattr(companions.shutil, "which", lambda name: None)
    assert companions.companion_bin("gh-axi") is None


# --------------------------------------------------------------------------- #
# Treehouse provisioning
# --------------------------------------------------------------------------- #


def test_provision_treehouse_verifies_and_activates(ppy_home, monkeypatch) -> None:
    lock = _fake_lock()
    monkeypatch.setattr(provision, "platform_slug", lambda: "darwin-arm64")
    monkeypatch.setattr(provision, "_download", lambda url, dest: _write_treehouse_archive(dest))
    monkeypatch.setattr(provision, "_sha256", lambda path: "DEADBEEF")  # matches the fake lock

    result = provision.provision_treehouse(lock, force=False)
    assert result.status == "installed", result.detail
    link = tools_bin_dir() / "treehouse"
    assert link.exists()
    # The resolver now sees the managed copy.
    assert companions.companion_bin("treehouse") == str(link)
    assert os.access(link, os.X_OK)


def test_provision_treehouse_rejects_bad_checksum(ppy_home, monkeypatch) -> None:
    lock = _fake_lock()
    monkeypatch.setattr(provision, "platform_slug", lambda: "darwin-arm64")
    monkeypatch.setattr(provision, "_download", lambda url, dest: _write_treehouse_archive(dest))
    monkeypatch.setattr(provision, "_sha256", lambda path: "NOTBEEF")

    result = provision.provision_treehouse(lock, force=False)
    assert result.status == "failed"
    assert "checksum" in result.detail
    assert not (tools_bin_dir() / "treehouse").exists()


# --------------------------------------------------------------------------- #
# npm provisioning
# --------------------------------------------------------------------------- #


def test_provision_npm_tool_activates_bin(ppy_home, monkeypatch) -> None:
    lock = _fake_lock()

    def fake_npm(name: str, version: str, prefix: Path) -> None:
        bindir = prefix / "node_modules" / ".bin"
        bindir.mkdir(parents=True, exist_ok=True)
        exe = bindir / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)

    monkeypatch.setattr(provision.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(provision, "_npm_install", fake_npm)

    result = provision.provision_npm_tool("lavish-axi", lock, force=False)
    assert result.status == "installed", result.detail
    assert (tools_bin_dir() / "lavish-axi").exists()


def test_provision_npm_tool_requires_npm(ppy_home, monkeypatch) -> None:
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)
    result = provision.provision_npm_tool("gh-axi", _fake_lock(), force=False)
    assert result.status == "failed"
    assert "npm" in result.detail


# --------------------------------------------------------------------------- #
# Orchestration never crashes
# --------------------------------------------------------------------------- #


def test_provision_all_is_crash_safe(ppy_home, monkeypatch) -> None:
    def boom(lock, *, force=False):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(provision._PROVISIONERS, "treehouse", boom)
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)  # npm tools fail cleanly

    results = provision.provision_all(lock=_fake_lock())
    assert {r.name for r in results} == {"treehouse", "lavish-axi", "gh-axi"}
    assert all(r.status == "failed" for r in results)  # none raised


def test_unknown_companion_reported(ppy_home) -> None:
    results = provision.provision_all(names=["bogus"], lock=_fake_lock())
    assert results[0].status == "failed"
    assert "unknown" in results[0].detail


def test_cli_tools_install_unknown_returns_nonzero(ppy_home) -> None:
    from papaya_agent_runtime.cli import main

    # Reads the real tools.lock but never hits the network for an unknown name.
    assert main(["tools", "install", "bogus"]) == 1
