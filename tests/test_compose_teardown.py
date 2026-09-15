"""A task's per-task compose stack goes down when the task is over.

On 2026-09-03 fifteen abandoned stacks — every one belonging to a delivered task —
were still up with their volumes, and Docker hit its 33-network ceiling: new
stacks stopped starting, so backend workers could no longer bring up the database
their own tests needed. Nothing in the harness had ever torn one down.

The docker subprocess is faked throughout: these tests assert the exact command
that would run, and that a machine with no docker at all never fails the command
it was attached to.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from papaya_agent_runtime import compose, delivery, lifecycle, progress, repos
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.state.db import _column_names
from papaya_agent_runtime.worktree.lease import LeaseManager
from papaya_agent_runtime.worktree.reclaim import prune


@pytest.fixture
def repo(ppy_home, source_repo):
    return repos.add_repo(source_repo)


@pytest.fixture
def fake_docker(monkeypatch):
    """Stand in for the docker daemon; record every argv the code runs."""

    class Docker:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.stdout = ""
            self.stderr = (
                " Container task_1-db-1  Removed\n"
                " Volume task_1_pgdata  Removed\n"
                " Network task_1_default  Removed\n"
            )
            self.returncode = 0

        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

        @property
        def down_calls(self) -> list[list[str]]:
            return [c for c in self.calls if "down" in c]

    docker = Docker()
    monkeypatch.setattr(compose, "docker_bin", lambda: "/usr/local/bin/docker")
    monkeypatch.setattr(compose, "_run", docker)
    return docker


def _task(repo, *, status: str = "in_progress", title: str = "backend work") -> int:
    conn = init_db()
    run_id = store.create_run(conn, title)
    repo_row = store.get_repo(conn, repo.name)
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_row["id"])
    store.set_task_status(conn, task_id, status)
    return task_id


def _leased_task(repo, *, status: str):
    conn = init_db()
    task_id = _task(repo, status="in_progress")
    repo_row = store.get_repo(conn, repo.name)
    lease = LeaseManager("git").acquire(
        repo_path=repo_row["local_path"], repo_id=repo_row["id"], task_id=task_id
    )
    store.update_task_fields(
        conn,
        task_id,
        branch=lease.branch,
        worktree_path=lease.worktree_path,
        lease_id=lease.id,
        base_sha=lease.base_sha,
    )
    store.set_task_status(conn, task_id, status)
    return task_id, lease


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# --------------------------------------------------------------------------- #
# Recording which stack belongs to which task
# --------------------------------------------------------------------------- #


def test_schema_gains_task_env_on_fresh_and_existing_databases(ppy_home) -> None:
    conn = init_db()
    assert _column_names(conn, "task_env") == {
        "task_id",
        "key",
        "value",
        "source",
        "created_at",
        "updated_at",
    }
    init_db()  # idempotent


def test_task_env_set_and_show_round_trip(repo, capsys):
    task_id = _task(repo)

    assert main(["task", "env", "set", str(task_id), "compose_project=task_7"]) == 0
    assert compose.project_for(task_id) == "task_7"

    capsys.readouterr()
    assert main(["task", "env", "show", str(task_id)]) == 0
    assert "compose_project = task_7" in capsys.readouterr().out


def test_task_env_set_replaces_an_earlier_value(repo):
    task_id = _task(repo)
    compose.record_project(task_id, "task_7")
    compose.record_project(task_id, "papaya_task_7")
    assert compose.project_for(task_id) == "papaya_task_7"
    assert len(store.task_env(init_db(), task_id)) == 1


def test_task_env_refuses_a_name_docker_would_not_take(repo, capsys):
    task_id = _task(repo)
    assert main(["task", "env", "set", str(task_id), "compose_project=not a name"]) == 1
    assert "compose project name" in capsys.readouterr().err


def test_task_env_wants_key_equals_value(repo, capsys):
    task_id = _task(repo)
    assert main(["task", "env", "set", str(task_id), "compose_project"]) == 1
    assert "key=value" in capsys.readouterr().err


def test_a_progress_note_naming_this_tasks_own_stack_records_it(repo):
    task_id = _task(repo)
    progress.record(
        task_id,
        phase="implement",
        note=f"brought the database up with COMPOSE_PROJECT_NAME=task_{task_id} on port 55432",
    )
    assert compose.project_for(task_id) == f"task_{task_id}"
    assert store.task_env(init_db(), task_id)[0]["source"] == "progress-note"


def test_a_prefixed_per_task_name_is_this_tasks_own_stack_too(repo):
    task_id = _task(repo)
    progress.record(task_id, phase="implement", note=f"COMPOSE_PROJECT_NAME=papaya_task_{task_id}")
    assert compose.project_for(task_id) == f"papaya_task_{task_id}"


def test_a_progress_note_without_a_stack_records_nothing(repo):
    task_id = _task(repo)
    progress.record(task_id, phase="implement", note="ran the suite; all green")
    assert compose.project_for(task_id) is None


# --------------------------------------------------------------------------- #
# A note is prose, and what it arms is `down -v`
#
# This machine runs shared stacks (chat-with-agents) and the user's own
# (radar_phase6_7) beside the per-task ones. A worker that merely *mentions* one
# must never arm a teardown that would take its volumes with it.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shared", ["chat-with-agents", "radar_phase6_7", "papaya-backend"])
def test_a_note_naming_a_shared_stack_is_ignored(repo, shared):
    task_id = _task(repo)

    progress.record(
        task_id, phase="implement", note=f"pointed at the shared COMPOSE_PROJECT_NAME={shared}"
    )

    assert compose.project_for(task_id) is None
    ignored = _events(task_id, "compose_project_ignored")
    assert [e["project"] for e in ignored] == [shared]
    assert "not task" in ignored[0]["summary"]
    assert f"ppy task env set {task_id}" in ignored[0]["summary"]


def test_a_note_naming_another_tasks_stack_is_ignored(repo):
    other = _task(repo, title="somebody else's task")
    task_id = _task(repo)

    progress.record(task_id, phase="implement", note=f"COMPOSE_PROJECT_NAME=task_{other}")

    assert compose.project_for(task_id) is None
    assert compose.project_for(other) is None
    assert [e["project"] for e in _events(task_id, "compose_project_ignored")] == [f"task_{other}"]


def test_an_ignored_note_arms_no_teardown_at_all(repo, fake_docker):
    task_id = _task(repo, status="worker_done")
    progress.record(task_id, phase="implement", note="COMPOSE_PROJECT_NAME=chat-with-agents")

    res = delivery.record_merged(task_id, "f" * 40)

    assert res.compose is None
    assert fake_docker.calls == [], "a mentioned stack is somebody's running work"


def test_the_manager_can_still_set_a_name_a_note_would_not_have_been_trusted_for(repo):
    """`ppy task env set` is a decision, not a mention, so it stays free-form."""
    task_id = _task(repo)
    assert main(["task", "env", "set", str(task_id), "compose_project=chat-with-agents"]) == 0
    assert compose.project_for(task_id) == "chat-with-agents"


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("COMPOSE_PROJECT_NAME=task_9", "task_9"),
        ('export COMPOSE_PROJECT_NAME="papaya_task_9"', "papaya_task_9"),
        ("COMPOSE_PROJECT_NAME: task_9", "task_9"),
        ("no stack here", None),
    ],
)
def test_detect_project_reads_the_ways_a_worker_writes_it(note, expected):
    assert compose.detect_project(note) == expected


# --------------------------------------------------------------------------- #
# Teardown on the three moments a task's worktree is given up
# --------------------------------------------------------------------------- #


def test_deliver_merged_takes_the_stack_down(repo, fake_docker):
    task_id = _task(repo, status="worker_done")
    compose.record_project(task_id, "task_1")

    res = delivery.record_merged(task_id, "a" * 40)

    assert fake_docker.down_calls == [
        [
            "/usr/local/bin/docker",
            "compose",
            "-p",
            "task_1",
            "down",
            "-v",
            "--remove-orphans",
        ]
    ]
    assert res.compose["ok"] is True
    assert res.compose["removed"] == [
        "Container task_1-db-1  Removed",
        "Volume task_1_pgdata  Removed",
        "Network task_1_default  Removed",
    ]
    assert _events(task_id, "compose_down")[0]["trigger"] == "deliver --merged"


def test_task_close_takes_the_stack_down(repo, fake_docker):
    task_id = _task(repo, status="blocked")
    compose.record_project(task_id, "task_2")

    res = lifecycle.close_task(task_id, "superseded by the user's own change")

    assert fake_docker.down_calls[0][2:4] == ["-p", "task_2"]
    assert res["compose"]["ok"] is True
    assert _events(task_id, "task_closed")[0]["compose_project"] == "task_2"


def test_worktree_prune_takes_the_stack_of_the_slot_it_reclaims_down(repo, fake_docker):
    task_id, lease = _leased_task(repo, status="delivered")
    compose.record_project(task_id, "task_3")

    res = prune()

    assert [r["task_id"] for r in res["removed"]] == [task_id]
    assert not Path(lease.worktree_path).exists()
    assert fake_docker.down_calls[0][2:4] == ["-p", "task_3"]
    assert [s["trigger"] for s in res["compose"]] == ["worktree prune"]


def test_a_slot_that_is_kept_keeps_its_stack(repo, fake_docker):
    task_id, lease = _leased_task(repo, status="delivered")
    compose.record_project(task_id, "task_4")
    Path(lease.worktree_path, "unsaved.txt").write_text("not committed anywhere\n")

    res = prune()

    assert res["removed"] == []
    assert fake_docker.down_calls == []
    assert res["compose"] == []


def test_a_dry_run_takes_nothing_down(repo, fake_docker):
    task_id, _lease = _leased_task(repo, status="delivered")
    compose.record_project(task_id, "task_5")

    prune(dry_run=True)

    assert fake_docker.down_calls == []


def test_a_task_with_no_recorded_stack_runs_no_docker_at_all(repo, fake_docker):
    task_id = _task(repo, status="worker_done")
    res = delivery.record_merged(task_id, "b" * 40)
    assert res.compose is None
    assert fake_docker.calls == []


# --------------------------------------------------------------------------- #
# Docker being absent, broken, or already finished never fails the command
# --------------------------------------------------------------------------- #


def test_no_docker_on_the_machine_still_delivers(repo, monkeypatch, capsys):
    monkeypatch.setattr(compose, "docker_bin", lambda: None)
    task_id = _task(repo, status="worker_done")
    compose.record_project(task_id, "task_6")

    assert main(["deliver", str(task_id), "--merged", "c" * 40]) == 0
    out = capsys.readouterr().out
    assert "recorded as delivered" in out
    assert "docker is not installed" in out
    assert store.get_task(init_db(), task_id)["status"] == "delivered"


def test_a_stack_that_is_already_gone_is_not_an_error(repo, fake_docker):
    task_id = _task(repo, status="worker_done")
    compose.record_project(task_id, "task_7")
    fake_docker.stderr = ""

    res = delivery.record_merged(task_id, "d" * 40)

    assert res.compose["ok"] is True
    assert res.compose["removed"] == []
    assert "nothing left to remove" in compose.describe(res.compose)


def test_a_docker_failure_is_reported_and_the_task_still_closes(repo, fake_docker):
    task_id = _task(repo, status="blocked")
    compose.record_project(task_id, "task_8")
    fake_docker.returncode = 1
    fake_docker.stderr = "Cannot connect to the Docker daemon"

    res = lifecycle.close_task(task_id, "dropped on a scope call")

    assert res["status"] == "closed"
    assert res["compose"]["ok"] is False
    assert "Cannot connect" in compose.describe(res["compose"])
    assert store.get_task(init_db(), task_id)["status"] == "closed"


def test_docker_raising_outright_never_reaches_the_caller(repo, monkeypatch):
    def boom(argv):
        raise OSError("docker vanished mid-call")

    monkeypatch.setattr(compose, "docker_bin", lambda: "/usr/local/bin/docker")
    monkeypatch.setattr(compose, "_run", boom)
    task_id = _task(repo, status="worker_done")
    compose.record_project(task_id, "task_9")

    res = delivery.record_merged(task_id, "e" * 40)

    assert res.compose["ok"] is False
    assert "vanished" in res.compose["detail"]
    assert store.get_task(init_db(), task_id)["status"] == "delivered"


# --------------------------------------------------------------------------- #
# ppy health lists the stacks nobody needs any more
# --------------------------------------------------------------------------- #


@pytest.fixture
def docker_ls(monkeypatch):
    """A fake ``docker compose ls`` whose project list the test sets."""
    state: dict = {"projects": [], "returncode": 0}

    def run(argv):
        if "ls" in argv:
            return subprocess.CompletedProcess(
                argv, state["returncode"], json.dumps(state["projects"]), ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(compose, "docker_bin", lambda: "/usr/local/bin/docker")
    monkeypatch.setattr(compose, "_run", run)
    return state


def test_health_lists_stacks_whose_task_is_over(repo, docker_ls, capsys):
    done = _task(repo, status="delivered", title="delivered work")
    live = _task(repo, status="in_progress", title="live work")
    docker_ls["projects"] = [
        {"Name": f"task_{done}", "Status": "running(1)"},
        {"Name": f"papaya_task_{live}", "Status": "running(2)"},
        {"Name": "someone-elses-app", "Status": "running(3)"},
    ]

    stacks = compose.prunable_stacks()

    assert [s["project"] for s in stacks] == [f"task_{done}"]
    assert stacks[0]["task_status"] == "delivered"

    main(["health", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert [s["task_id"] for s in payload["prunable_compose_stacks"]] == [done]


def test_health_says_so_when_nothing_is_left_over(repo, docker_ls, capsys):
    docker_ls["projects"] = []
    main(["health"])
    assert "none left over from finished tasks" in capsys.readouterr().out


def test_health_prints_the_prunable_stacks_it_found(repo, docker_ls, capsys):
    done = _task(repo, status="closed")
    docker_ls["projects"] = [{"Name": f"task_{done}", "Status": "running(1)"}]
    main(["health"])
    out = capsys.readouterr().out
    assert f"task_{done} (task {done} closed)" in out
    assert "ppy worktree prune" in out


def test_health_survives_a_machine_with_no_docker(repo, monkeypatch, capsys):
    monkeypatch.setattr(compose, "docker_bin", lambda: None)
    assert compose.prunable_stacks() == []
    main(["health"])
    assert "none left over" in capsys.readouterr().out


def test_a_broken_docker_compose_ls_is_treated_as_no_stacks(repo, docker_ls):
    docker_ls["returncode"] = 1
    assert compose.list_stacks() == []
    assert compose.prunable_stacks() == []


@pytest.mark.parametrize(
    ("project", "task_id"),
    [("task_12", 12), ("papaya_task_12", 12), ("task_12_extra", None), ("mytask_12", None)],
)
def test_task_id_of_reads_the_project_naming_convention(project, task_id):
    assert compose.task_id_of(project) == task_id
