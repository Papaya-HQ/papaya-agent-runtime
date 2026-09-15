"""A leased worktree is ready before the worker sees it.

Three separate workers in the 2026-08-31 window recorded the same largest
remaining cost per dispatch: roughly ten minutes before touching the task, spent
building a virtualenv the base clone already had, re-filling a dependency cache
from scratch, and escalating out of the sandbox to write anywhere at all.

The environment half is unconditional. The per-repo half is opt-in, so the first
thing these tests pin down is that a repo with nothing configured is untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from papaya_agent_runtime import repos
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.paths import ppy_home, uv_cache_dir
from papaya_agent_runtime.repos import RepoError
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.state.db import _column_names
from papaya_agent_runtime.supervisor.runner import worker_env
from papaya_agent_runtime.worktree.lease import LeaseManager
from papaya_agent_runtime.worktree.provision import (
    ProvisionResult,
    link_venv,
    provision_worktree,
    run_hook,
    timeout_seconds,
)


@pytest.fixture
def repo(ppy_home, source_repo):
    return repos.add_repo(source_repo)


def _repo_row(name):
    return store.get_repo(init_db(), name)


def _lease(repo):
    row = _repo_row(repo.name)
    return LeaseManager("git").acquire(repo_path=row["local_path"], repo_id=row["id"])


def _events(kind):
    conn = init_db()
    rows = conn.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _make_venv(root: Path, relative: str) -> Path:
    venv = root / relative
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "pyvenv.cfg").write_text(f"home = {root}\n")
    return venv


# --------------------------------------------------------------------------- #
# The environment every worker gets, whatever the repo says
# --------------------------------------------------------------------------- #


def test_worker_env_exports_a_writable_uv_cache_under_the_ppy_home(ppy_home):
    env = worker_env({"PATH": "/usr/bin"})

    cache = Path(env["UV_CACHE_DIR"])
    assert cache == uv_cache_dir()
    assert cache.is_relative_to(ppy_home)
    assert cache.is_dir(), "a cache path a sandbox cannot create is no better than none"
    assert os.access(cache, os.W_OK)
    probe = cache / "probe"
    probe.write_text("writable\n")
    assert probe.read_text() == "writable\n"


def test_worker_env_exports_a_writable_ppy_home(ppy_home):
    env = worker_env({"PATH": "/usr/bin"})

    home = Path(env["PPY_HOME"])
    assert home == ppy_home
    assert home.is_dir()
    assert os.access(home, os.W_OK), "ppy progress must not need a sandbox escalation"


def test_the_uv_cache_is_shared_across_tasks_so_the_second_dispatch_is_warm(ppy_home):
    assert worker_env({})["UV_CACHE_DIR"] == worker_env({})["UV_CACHE_DIR"]


def test_worker_env_still_pins_ppy_home_and_path(ppy_home):
    env = worker_env({"PATH": "/usr/bin"})
    assert env["PATH"].endswith("/usr/bin")
    assert env["PATH"].split(os.pathsep)[0].endswith("/bin")


# --------------------------------------------------------------------------- #
# Per-repo configuration
# --------------------------------------------------------------------------- #


def test_schema_gains_the_provision_columns_on_fresh_and_existing_databases(ppy_home):
    conn = init_db()
    assert {"provision_command", "provision_venv"} <= _column_names(conn, "repos")
    conn.execute("ALTER TABLE repos DROP COLUMN provision_command")
    conn.commit()
    assert "provision_command" not in _column_names(conn, "repos")
    conn = init_db()
    assert "provision_command" in _column_names(conn, "repos")
    init_db()  # a second run must not fail on the now-present column


def test_a_repo_starts_with_nothing_configured(repo):
    settings = repos.get_provision(repo.name)
    assert settings.configured is False
    assert "no worktree provisioning" in settings.describe()


def test_provision_settings_round_trip_through_the_cli(repo, capsys):
    assert (
        main(
            [
                "repo",
                "provision",
                repo.name,
                "--command",
                "uv sync --frozen",
                "--reuse-venv",
                "backend/.venv",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "reuses backend/.venv" in out
    assert "uv sync --frozen" in out

    settings = repos.get_provision(repo.name)
    assert settings.command == "uv sync --frozen"
    assert settings.reuse_venv == "backend/.venv"

    assert main(["repo", "provision", repo.name]) == 0
    assert "uv sync --frozen" in capsys.readouterr().out


def test_provision_can_be_turned_off(repo):
    repos.set_provision(repo.name, command="uv sync", reuse_venv="backend/.venv")
    assert repos.set_provision(repo.name, clear=True).configured is False


def test_one_field_clears_without_touching_the_other(repo):
    repos.set_provision(repo.name, command="uv sync", reuse_venv="backend/.venv")
    settings = repos.set_provision(repo.name, command="")
    assert settings.command is None
    assert settings.reuse_venv == "backend/.venv"


@pytest.mark.parametrize("bad", ["/absolute/.venv", "../escape/.venv"])
def test_reuse_venv_refuses_a_path_outside_the_repo(repo, bad):
    with pytest.raises(RepoError):
        repos.set_provision(repo.name, reuse_venv=bad)


def test_provision_on_an_unregistered_repo_is_refused(ppy_home, capsys):
    assert main(["repo", "provision", "nope", "--command", "true"]) == 1
    assert "not registered" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Reusing the base clone's virtualenv
# --------------------------------------------------------------------------- #


def test_the_base_clones_venv_is_linked_into_the_worktree(repo):
    base = _repo_row(repo.name)["local_path"]
    source = _make_venv(Path(base), "backend/.venv")
    repos.set_provision(repo.name, reuse_venv="backend/.venv")
    lease = _lease(repo)

    result = provision_worktree(_repo_row(repo.name), lease.worktree_path)

    linked = Path(lease.worktree_path, "backend/.venv")
    assert result.venv["linked"] is True
    assert result.venv["how"] == "symlinked"
    assert linked.is_symlink()
    assert linked.resolve() == source.resolve()
    assert (linked / "bin" / "python").exists(), "the worktree can run the base clone's python"


def test_the_linked_venv_is_invisible_to_git_so_it_never_lands_in_a_commit(repo):
    _make_venv(Path(_repo_row(repo.name)["local_path"]), "backend/.venv")
    repos.set_provision(repo.name, reuse_venv="backend/.venv")
    lease = _lease(repo)

    provision_worktree(_repo_row(repo.name), lease.worktree_path)

    status = subprocess.run(
        ["git", "-C", lease.worktree_path, "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ".venv" not in status


def test_a_missing_venv_in_the_base_clone_is_reported_not_fatal(repo):
    repos.set_provision(repo.name, reuse_venv="backend/.venv")
    lease = _lease(repo)

    result = provision_worktree(_repo_row(repo.name), lease.worktree_path)

    assert result.venv["linked"] is False
    assert "no backend/.venv to reuse" in result.venv["detail"]
    assert Path(lease.worktree_path, "README.md").exists()


def test_a_venv_already_in_the_worktree_is_left_alone(repo):
    _make_venv(Path(_repo_row(repo.name)["local_path"]), ".venv")
    lease = _lease(repo)
    existing = _make_venv(Path(lease.worktree_path), ".venv")

    result = link_venv(_repo_row(repo.name)["local_path"], lease.worktree_path, ".venv")

    assert result["linked"] is False
    assert "already in the worktree" in result["detail"]
    assert not existing.is_symlink()


def test_a_filesystem_that_refuses_symlinks_falls_back_to_a_copy(repo, monkeypatch):
    base = _repo_row(repo.name)["local_path"]
    _make_venv(Path(base), "backend/.venv")
    lease = _lease(repo)

    def no_symlinks(self, target, target_is_directory=False):
        raise OSError("symlinks are not supported here")

    monkeypatch.setattr(Path, "symlink_to", no_symlinks)
    result = link_venv(base, lease.worktree_path, "backend/.venv")

    assert result["how"] == "copied"
    assert Path(lease.worktree_path, "backend/.venv/bin/python").exists()


# --------------------------------------------------------------------------- #
# The per-repo provision hook
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_hook(monkeypatch):
    """Stand in for the hook subprocess; record what would have run."""
    from papaya_agent_runtime.worktree import provision

    calls: list[dict] = []
    state = {"returncode": 0, "stdout": "Resolved 84 packages\n", "stderr": ""}

    def run(command, cwd, env):
        calls.append({"command": command, "cwd": cwd, "env": env})
        return subprocess.CompletedProcess(command, state["returncode"], state["stdout"], "")

    monkeypatch.setattr(provision, "_run", run)
    return calls, state


def test_the_hook_runs_in_the_new_worktree_before_the_worker(repo, fake_hook):
    calls, _state = fake_hook
    repos.set_provision(repo.name, command="uv sync --frozen")
    lease = _lease(repo)

    result = provision_worktree(_repo_row(repo.name), lease.worktree_path, task_id=None)

    assert [c["command"] for c in calls] == ["uv sync --frozen"]
    assert calls[0]["cwd"] == lease.worktree_path
    assert result.hook["exit_code"] == 0
    assert "Resolved 84 packages" in result.hook["output"]


def test_the_hook_gets_the_worker_environment(repo, fake_hook):
    calls, _state = fake_hook
    repos.set_provision(repo.name, command="uv sync")
    lease = _lease(repo)

    provision_worktree(_repo_row(repo.name), lease.worktree_path)

    env = calls[0]["env"]
    assert env["UV_CACHE_DIR"] == str(uv_cache_dir())
    assert env["PPY_HOME"] == str(ppy_home())


def test_the_hook_records_an_event_with_its_exit_status(repo, fake_hook):
    _calls, _state = fake_hook
    repos.set_provision(repo.name, command="uv sync")
    lease = _lease(repo)
    conn = init_db()
    run_id = store.create_run(conn, "provisioned run")
    task_id = store.add_task(conn, run_id=run_id, title="work")

    provision_worktree(_repo_row(repo.name), lease.worktree_path, task_id=task_id, run_id=run_id)

    payload = _events("worktree_provisioned")[0]
    assert payload["task_id"] == task_id
    assert payload["repo"] == repo.name
    assert payload["hook"]["exit_code"] == 0


def test_a_failing_hook_is_recorded_and_the_dispatch_goes_on(repo, fake_hook):
    _calls, state = fake_hook
    state["returncode"] = 3
    state["stdout"] = "error: no lockfile\n"
    repos.set_provision(repo.name, command="uv sync --frozen")
    lease = _lease(repo)

    result = provision_worktree(_repo_row(repo.name), lease.worktree_path)

    assert result.hook["exit_code"] == 3
    assert "the worker starts anyway" in result.hook["detail"]
    assert _events("worktree_provisioned")[0]["hook"]["exit_code"] == 3


def test_a_hook_that_hangs_is_given_up_on(repo, monkeypatch):
    from papaya_agent_runtime.worktree import provision

    def hang(command, cwd, env):
        raise subprocess.TimeoutExpired(command, timeout_seconds())

    monkeypatch.setattr(provision, "_run", hang)
    result = run_hook("sleep forever", str(ppy_home()))

    assert result["ran"] is False
    assert "timed out" in result["detail"]


def test_the_hook_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("PPY_PROVISION_TIMEOUT", "42")
    assert timeout_seconds() == 42
    monkeypatch.setenv("PPY_PROVISION_TIMEOUT", "not a number")
    assert timeout_seconds() == 900


def test_the_hook_really_runs_a_command(repo):
    """One end-to-end pass, so the shell plumbing is not only ever faked."""
    repos.set_provision(repo.name, command="echo provisioned > receipt.txt")
    lease = _lease(repo)

    result = provision_worktree(_repo_row(repo.name), lease.worktree_path)

    assert result.hook["exit_code"] == 0
    assert Path(lease.worktree_path, "receipt.txt").read_text().strip() == "provisioned"


# --------------------------------------------------------------------------- #
# A repo with nothing configured behaves exactly as it did before
# --------------------------------------------------------------------------- #


def test_an_unconfigured_repo_provisions_nothing_and_records_nothing(repo, fake_hook):
    calls, _state = fake_hook
    lease = _lease(repo)

    result = provision_worktree(_repo_row(repo.name), lease.worktree_path)

    assert isinstance(result, ProvisionResult)
    assert result.configured is False
    assert calls == []
    assert _events("worktree_provisioned") == []
    assert sorted(p.name for p in Path(lease.worktree_path).iterdir()) == [".git", "README.md"]


def test_dispatch_provisions_the_worktree_before_the_worker_runs(ppy_home, source_repo):
    """End to end: the real dispatch path, a real hook, a real lease."""
    import time

    from papaya_agent_runtime.supervisor.client import SupervisorClient
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    added = repos.add_repo(source_repo)
    _make_venv(Path(_repo_row(added.name)["local_path"]), "backend/.venv")
    repos.set_provision(
        added.name, command="echo ready > provisioned.txt", reuse_venv="backend/.venv"
    )

    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    try:
        for _ in range(50):
            try:
                if client.ping().get("ok"):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        resp = client.dispatch_task(repo=added.name, title="provisioned work")
        assert resp["ok"], resp
        worktree = Path(resp["worktree_path"])
        assert Path(worktree, "provisioned.txt").read_text().strip() == "ready"
        assert Path(worktree, "backend/.venv").is_symlink()
        payload = _events("worktree_provisioned")[0]
        assert payload["task_id"] == resp["task_id"]
        assert payload["hook"]["exit_code"] == 0
        assert payload["venv"]["linked"] is True
    finally:
        srv.stop()


def test_provisioning_that_blows_up_never_reaches_the_dispatch(repo, monkeypatch):
    from papaya_agent_runtime.worktree import provision

    def boom(*a, **k):
        raise RuntimeError("the provisioner fell over")

    repos.set_provision(repo.name, reuse_venv="backend/.venv")
    monkeypatch.setattr(provision, "link_venv", boom)
    lease = _lease(repo)

    result = provision_worktree(_repo_row(repo.name), lease.worktree_path)

    assert "provisioning skipped" in result.hook["detail"]
