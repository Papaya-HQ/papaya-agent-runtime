"""Dispatch refuses overlapping in-flight work and stacking onto an empty parent (runtime #94).

The overlap check (issue #61) only printed a suggestion, and the unattended
``ppy serve`` manager has nobody to read one: this repository had three tasks
editing ``serve.py`` at once (task 243's reflection). In Middle Manager a child was
also stacked on a parent whose branch had no commits yet, so it started from — and
recorded — the default branch as its base. Both are now refused before any task
state exists, unless waved through with ``--accept-preflight overlap|empty-parent
--reason ...``. The brief preflight (allowlist, prior attempt) is wired into
``ppy dispatch --brief`` and ``ppy brief lint`` here too.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import namedtuple

import pytest

from conftest import wait_until
from papaya_agent_runtime import brief_lint, cli, overlap, preflight, repos
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer
from papaya_agent_runtime.worktree import LeaseManager

ACCEPT_OVERLAP = [{"check": "overlap", "overridden": None}]
Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def server(ppy_home):
    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield srv, client
    srv.stop()


@pytest.fixture
def registered(server, source_repo):
    return repos.add_repo(source_repo)


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(path, name: str) -> str:
    with open(f"{path}/{name}", "w", encoding="utf-8") as fh:
        fh.write(name + "\n")
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"add {name}")
    return _git(path, "rev-parse", "HEAD")


def _in_progress_task(repo_name: str, title: str, *, touches: list[str] | None = None) -> int:
    """A task still being worked on: a real lease worktree whose branch has no commits yet."""
    conn = init_db()
    repo_row = store.get_repo(conn, repo_name)
    run_id = store.create_run(conn, title)
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_row["id"])
    lease = LeaseManager().acquire(
        repo_path=repo_row["local_path"], repo_id=repo_row["id"], task_id=task_id
    )
    store.update_task_fields(
        conn,
        task_id,
        branch=lease.branch,
        worktree_path=lease.worktree_path,
        base_sha=lease.base_sha,
        lease_id=lease.id,
    )
    store.set_task_status(conn, task_id, "in_progress")
    overlap.record_touches(conn, task_id, touches or [])
    return task_id


def _task(task_id: int):
    return store.get_task(init_db(), task_id)


def _task_count() -> int:
    return init_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _events(task_id: int, kind: str) -> list[dict]:
    rows = init_db().execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    )
    return [json.loads(row["payload"]) for row in rows]


def _wait_terminal(client, task_id, timeout=15.0):
    def finished():
        status = client.task_status(task_id)["task"]["status"]
        return status if status in {"worker_done", "blocked", "failed"} else None

    return wait_until(finished, timeout, what=f"task {task_id} to finish", interval=0.1)


def _dispatch(client, repo: str, touches: str, **kwargs) -> dict:
    return client.dispatch_task(
        repo=repo, title="next change", instructions=f"# Next\n\nTouches: {touches}\n", **kwargs
    )


# --------------------------------------------------------------------------- #
# G1: overlapping in-flight work is refused
# --------------------------------------------------------------------------- #


def test_an_overlapping_dispatch_is_refused_naming_the_sibling_and_the_stack_on_to_use(
    server, registered
) -> None:
    _srv, client = server
    sibling = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])
    before = _task_count()

    resp = _dispatch(client, registered.name, "src/serve.py, tests/test_serve.py")

    assert not resp["ok"]
    error = resp["error"]
    assert error.startswith("dispatch refused:")
    assert f'task {sibling} "rework serve"' in error
    assert "src/serve.py" in error
    assert f"--stack-on {sibling}" in error
    assert "--accept-preflight overlap" in error
    assert "Nothing was refused" not in error
    assert _task_count() == before, "a refused dispatch must not create a task row"


def test_stacking_on_the_overlapping_task_passes(server, registered) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])
    _commit(_task(parent)["worktree_path"], "serve.txt")

    resp = _dispatch(client, registered.name, "src/serve.py", stack_on=parent)

    assert resp["ok"], resp
    assert resp["overlap_advisory"] is None
    _wait_terminal(client, resp["task_id"])


def test_stacking_on_another_task_is_refused_for_the_overlapping_sibling(
    server, registered
) -> None:
    _srv, client = server
    sibling = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])
    parent = _in_progress_task(registered.name, "docs", touches=["docs/serve.md"])
    _commit(_task(parent)["worktree_path"], "docs.txt")
    before = _task_count()

    resp = _dispatch(client, registered.name, "src/serve.py", stack_on=parent)

    assert not resp["ok"]
    assert f"--stack-on {sibling}" in resp["error"]
    assert f"--stack-on {parent}" not in resp["error"]
    assert _task_count() == before


def test_an_accepted_overlap_passes_and_records_what_it_waved_through(server, registered) -> None:
    _srv, client = server
    sibling = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])

    resp = _dispatch(
        client,
        registered.name,
        "src/serve.py",
        accepted_preflight=ACCEPT_OVERLAP,
        preflight_reason="x",
    )

    assert resp["ok"], resp
    assert f"--stack-on {sibling}" in resp["overlap_advisory"]
    [event] = _events(resp["task_id"], "preflight_accepted")
    assert event["check"] == "overlap"
    assert event["reason"] == "x"
    assert f'task {sibling} "rework serve"' in event["overridden"]
    _wait_terminal(client, resp["task_id"])


@pytest.mark.parametrize("status", ["delivered", "merged", "closed"])
def test_a_task_no_longer_in_flight_never_causes_an_overlap_refusal(
    server, registered, status
) -> None:
    _srv, client = server
    finished = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])
    store.set_task_status(init_db(), finished, status)

    resp = _dispatch(client, registered.name, "src/serve.py")

    assert resp["ok"], resp
    assert resp["overlap_advisory"] is None
    _wait_terminal(client, resp["task_id"])


# --------------------------------------------------------------------------- #
# G2: a stack parent with nothing on its branch is refused
# --------------------------------------------------------------------------- #


def test_stacking_on_a_parent_with_no_commits_beyond_its_base_is_refused(
    server, registered
) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")
    before = _task_count()

    resp = _dispatch(client, registered.name, "src/child.py", stack_on=parent)

    assert not resp["ok"]
    error = resp["error"]
    assert error.startswith("dispatch refused:")
    assert f'task {parent} "parent layer"' in error
    assert "no commits beyond its own base" in error
    assert "first push" in error
    assert "--accept-preflight empty-parent" in error
    assert _task_count() == before


def test_base_naming_an_in_flight_task_s_empty_lease_branch_is_refused(server, registered) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")
    before = _task_count()

    resp = _dispatch(client, registered.name, "src/child.py", base=_task(parent)["branch"])

    assert not resp["ok"]
    assert "no commits beyond its own base" in resp["error"]
    assert _task_count() == before


def test_a_parent_commit_in_its_lease_worktree_is_enough_to_stack_on(server, registered) -> None:
    """Committed, never pushed: the lease worktree alone carries the progress."""
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")
    head = _commit(_task(parent)["worktree_path"], "parent.txt")

    resp = _dispatch(client, registered.name, "src/child.py", stack_on=parent)

    assert resp["ok"], resp
    assert _task(resp["task_id"])["base_sha"] == head
    _wait_terminal(client, resp["task_id"])


def test_a_parent_commit_on_the_forge_is_enough_to_stack_on(server, registered) -> None:
    """Pushed, then the lease worktree moved back to base: only the forge has it."""
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")
    row = _task(parent)
    head = _commit(row["worktree_path"], "parent.txt")
    _git(row["worktree_path"], "push", "-q", "origin", f"HEAD:{row['branch']}")
    _git(row["worktree_path"], "reset", "-q", "--hard", row["base_sha"])

    resp = _dispatch(client, registered.name, "src/child.py", stack_on=parent)

    assert resp["ok"], resp
    assert _task(resp["task_id"])["base_sha"] == head
    _wait_terminal(client, resp["task_id"])


def test_an_accepted_empty_parent_passes_and_records_the_event(server, registered) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")

    resp = _dispatch(
        client,
        registered.name,
        "src/child.py",
        stack_on=parent,
        accepted_preflight=[{"check": "empty-parent", "overridden": None}],
        preflight_reason="parent is a placeholder",
    )

    assert resp["ok"], resp
    [event] = _events(resp["task_id"], "preflight_accepted")
    assert event["check"] == "empty-parent"
    assert event["reason"] == "parent is a placeholder"
    assert "no commits beyond its own base" in event["overridden"]
    _wait_terminal(client, resp["task_id"])


# --------------------------------------------------------------------------- #
# G3: the CLI override joins the fixed set
# --------------------------------------------------------------------------- #


@pytest.fixture
def sent(monkeypatch):
    """A stand-in supervisor that records what the CLI would have dispatched."""
    received: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            received.update(kwargs)
            return {"ok": True, "task_id": 7, "run_id": 3, "branch": "ppy/task-7-abc"}

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 20e9, 80e9))
    return received


def test_the_sequencing_checks_are_named_overridable_checks() -> None:
    assert "overlap" in preflight.CHECKS
    assert "empty-parent" in preflight.CHECKS


@pytest.mark.parametrize("check", ["overlap", "empty-parent"])
def test_the_cli_accepts_the_sequencing_checks_by_name_and_needs_a_reason(
    ppy_home, source_repo, sent, check
) -> None:
    added = repos.add_repo(source_repo)
    argv = ["dispatch", "--repo", added.name, "--title", "t", "--instructions", "go"]

    assert cli.main([*argv, "--accept-preflight", check]) == 2
    assert not sent

    assert cli.main([*argv, "--accept-preflight", check, "--reason", "x"]) == 0
    assert {"check": check, "overridden": None} in sent["accepted_preflight"]
    assert sent["preflight_reason"] == "x"


def test_the_cli_prints_a_supervisor_refusal_as_a_refusal(
    ppy_home, source_repo, monkeypatch, capsys
) -> None:
    class RefusingClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            return {"ok": False, "error": "dispatch refused: work already in flight ..."}

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", RefusingClient)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 20e9, 80e9))
    added = repos.add_repo(source_repo)
    capsys.readouterr()  # drain the clone's start and done lines from setup

    rc = cli.main(["dispatch", "--repo", added.name, "--title", "t", "--instructions", "go"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("dispatch refused: work already in flight")
    assert "dispatch failed" not in err


# --------------------------------------------------------------------------- #
# G4: one allowlist matcher
# --------------------------------------------------------------------------- #


def test_the_gate_check_and_the_brief_allowlist_share_one_matcher() -> None:
    assert preflight._allowed_prefixes is brief_lint._allowed_prefixes
    assert preflight._covered is brief_lint._covered


# --------------------------------------------------------------------------- #
# G5: the brief preflight runs at dispatch and at `ppy brief lint`
# --------------------------------------------------------------------------- #

CLEAN_BRIEF = """# Package the CLI

## Goals

The wheel installs.

## Intent

Users install one thing.

## In scope

`packaging/`. Pre-authorised adjacent changes: none.

## Out of scope

No new commands.

## Steps

Unpack the fixture with `unzip fixtures/sample.zip`, then run `uv run pytest -q`.
"""


@pytest.fixture
def claude_tools(monkeypatch):
    """The Claude worker's allowlist, without reading this machine's config."""
    tools = ["Read", "Edit", "Bash(git:*)", "Bash(uv:*)"]
    import papaya_agent_runtime.providers.claude as claude_mod

    monkeypatch.setattr(claude_mod, "effective_allowed_tools", lambda: (tools, "test"))
    return tools


@pytest.fixture
def prior_closed(ppy_home, source_repo):
    """A registered repo with a closed task titled like CLEAN_BRIEF."""
    added = repos.add_repo(source_repo)
    conn = init_db()
    repo_row = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "Package the CLI")
    task_id = store.add_task(conn, run_id=run_id, title="Package the CLI", repo_id=repo_row["id"])
    store.set_task_status(conn, task_id, "closed")
    return added.name, task_id


def _brief_file(tmp_path) -> str:
    path = tmp_path / "brief.md"
    path.write_text(CLEAN_BRIEF)
    return str(path)


def test_dispatch_brief_prints_allowlist_and_prior_attempt_findings_as_warnings(
    prior_closed, claude_tools, sent, tmp_path, monkeypatch, capsys
) -> None:
    repo, prior = prior_closed
    monkeypatch.setattr(preflight, "trust_checks", lambda *a, **k: preflight.DispatchTrust())

    rc = cli.main(
        ["dispatch", "--repo", repo, "--provider", "claude", "--brief", _brief_file(tmp_path)]
    )

    assert rc == 0
    assert sent, "findings are warnings: the dispatch still goes ahead"
    err = capsys.readouterr().err
    assert "claude allowlist: `unzip fixtures/sample.zip`" in err
    assert "no Prior attempt section" in err
    assert f"task {prior}" in err


def test_dispatch_brief_strict_refuses_on_preflight_findings(
    prior_closed, claude_tools, sent, tmp_path, monkeypatch, capsys
) -> None:
    repo, _prior = prior_closed
    monkeypatch.setattr(preflight, "trust_checks", lambda *a, **k: preflight.DispatchTrust())

    rc = cli.main(
        [
            "dispatch",
            "--repo",
            repo,
            "--provider",
            "claude",
            "--brief",
            _brief_file(tmp_path),
            "--strict",
        ]
    )

    assert rc == 1
    assert not sent
    err = capsys.readouterr().err
    assert "claude allowlist" in err and "no Prior attempt section" in err
    assert "dispatch refused: --strict" in err


def test_brief_lint_shows_the_same_preflight_findings(
    prior_closed, claude_tools, tmp_path, capsys
) -> None:
    repo, prior = prior_closed

    rc = cli.main(["brief", "lint", _brief_file(tmp_path), "--repo", repo, "--provider", "claude"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "claude allowlist: `unzip fixtures/sample.zip`" in out
    assert f"task {prior}" in out
    # Without a repo there is no prior attempt to find; a Codex worker has no allowlist.
    assert cli.main(["brief", "lint", _brief_file(tmp_path), "--provider", "codex"]) == 0
