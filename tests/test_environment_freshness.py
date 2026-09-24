"""An environment built from an older lockfile is named, not ticked.

On 2026-09-24 a pull added `questionary` to uv.lock. `ppy setup`'s first step asked
only whether the Papaya client was in site-packages, printed its tick, and the run
crashed at the first prompt. A sync by `ppy` stamps the environment with a hash of
uv.lock, pyproject.toml and the pinned series (`envsync.STAMP`); these read that
stamp against a scratch checkout. The conftest fixture that makes every other test
see a current environment skips this module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from papaya_agent_runtime import envsync, readiness


@pytest.fixture
def checkout(tmp_path, monkeypatch) -> Path:
    """A checkout with a lockfile, a pinned series and a built environment with the client."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "uv.lock").write_text('version = 1\n[[package]]\nname = "papaya-agent-client"\n')
    (root / "pyproject.toml").write_text('[project]\nname = "papaya-agent-runtime"\n')
    (root / ".python-version").write_text("3.13\n")
    env = root / ".venv"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("#!/bin/sh\n")
    client = env / "lib" / "python3.13" / "site-packages" / "papaya_agent_client"
    client.mkdir(parents=True)
    (client / "__init__.py").write_text("")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(env))
    monkeypatch.setattr(readiness, "checkout_root", lambda: root)
    return root


def _stamp(root: Path) -> None:
    """Stamp the environment the way a successful `envsync.build` does."""
    wanted = envsync.wanted_stamp(str(root), envsync.pinned_python(str(root)))
    (root / ".venv" / envsync.STAMP).write_text(wanted + "\n")


@pytest.fixture
def quiet_machine(monkeypatch, ppy_home):
    """Everything but the environment present, as in `tests/test_readiness.py`."""
    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_config_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_repo_problems", lambda problems: None)
    return ppy_home


def test_the_stamp_follows_the_lockfile_and_the_project(checkout) -> None:
    root = str(checkout)
    assert envsync.stale(root, "3.13"), "an environment no `ppy` sync built has no stamp"

    _stamp(checkout)
    assert not envsync.stale(root, "3.13")

    (checkout / "uv.lock").write_text((checkout / "uv.lock").read_text() + 'name = "questionary"\n')
    assert envsync.stale(root, "3.13")

    _stamp(checkout)
    (checkout / "pyproject.toml").write_text('[project]\nname = "renamed"\n')
    assert envsync.stale(root, "3.13")


def test_the_pinned_series_is_read_the_way_the_launcher_reads_it(checkout) -> None:
    # bin/ppy: `tr -d '[:space:]' <.python-version`
    (checkout / ".python-version").write_text(" 3.13 \n\n")
    assert envsync.pinned_python(str(checkout)) == "3.13"
    (checkout / ".python-version").unlink()
    assert envsync.pinned_python(str(checkout)) is None


def test_an_environment_from_an_older_lockfile_is_not_ready(checkout, quiet_machine) -> None:
    _stamp(checkout)
    env = readiness.environment_path()
    assert readiness.environment_ready(env)
    assert readiness.check().problems == []

    (checkout / "uv.lock").write_text((checkout / "uv.lock").read_text() + 'name = "questionary"\n')

    assert readiness.environment_imports(env), "the client is still there"
    assert not readiness.environment_current(env)
    assert not readiness.environment_ready(env)
    (stale,) = readiness.check().problems
    assert stale.code == readiness.ENVIRONMENT_STALE
    assert stale.owner == readiness.RUNTIME and not stale.blocking
    assert "`./bin/ppy` command rebuilds it" in stale.fix


def test_an_environment_nothing_stamped_is_not_called_stale(checkout, quiet_machine) -> None:
    """CI's `uv sync` stamps nothing; readiness does not guess what it was built from."""
    env = readiness.environment_path()
    assert readiness.environment_current(env)
    assert readiness.check().problems == []


def test_a_missing_client_is_still_broken_whatever_the_stamp_says(checkout, quiet_machine) -> None:
    _stamp(checkout)
    client = checkout / ".venv" / "lib" / "python3.13" / "site-packages" / "papaya_agent_client"
    (client / "__init__.py").unlink()

    (broken,) = readiness.check().problems
    assert broken.code == readiness.ENVIRONMENT_BROKEN
    assert not readiness.environment_ready(readiness.environment_path())
