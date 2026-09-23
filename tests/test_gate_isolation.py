"""A gate runs in its own task's database, never the supervisor's or another task's (#47).

On 2026-09-17 two backend tasks' full gates and a review's baseline check ran the suite
at once against one test database, and each saw failures the others caused. The gate
already handed a task's rendered values to its process, but the repository had no
private stack declared, so those values held nothing private and every gate fell back
to the repository's default database.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import make_git_repo
from papaya_agent_runtime import compose, environment, gate, readiness, repos
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer

#: A gate that says which stack and database it was given, then passes.
ECHO_ENV = (
    'echo "project=$COMPOSE_PROJECT_NAME port=$PAPAYA_DB_PORT test_db=$TEST_DATABASE_URL '
    'db=$DATABASE_URL head=$(git rev-parse HEAD)"; echo "1 passed in 0.01s"'
)
TEMPLATE = "postgresql://u@localhost:{port}/test_{task_id}"


def _compose_repo(clone: Path) -> None:
    conn = init_db()
    store.add_repo(
        conn,
        name="backend",
        origin="https://github.com/acme/backend.git",
        local_path=str(clone),
        default_branch="main",
        base_sha="a" * 40,
    )
    conn.close()
    repos.set_settings(
        "backend",
        local_gate=ECHO_ENV,
        compose_stack="yes",
        db_port_base="6000",
        db_port_variable="PAPAYA_DB_PORT",
        test_db_url_template=TEMPLATE,
    )


def _task(worktree: Path) -> int:
    conn = init_db()
    try:
        repo_id = store.get_repo(conn, "backend")["id"]
        run_id = store.create_run(conn, "gate")
        task_id = store.add_task(conn, run_id=run_id, title="gate", repo_id=repo_id)
        store.update_task_fields(conn, task_id, worktree_path=str(worktree), status="in_progress")
        return task_id
    finally:
        conn.close()


def _results(task_id: int | None) -> list[dict]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id IS ? AND kind = ? ORDER BY id",
            (task_id, gate.GATE_RESULT),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
    finally:
        conn.close()


@pytest.fixture
def supervisor_env(monkeypatch):
    """The supervisor's own shell names the shared stack, as a manager's shell does."""
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "chat-with-agents")
    monkeypatch.setenv("PAPAYA_DB_PORT", "5433")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost:5433/lightwork")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u@localhost:5433/lightwork_test")


def test_a_gate_for_a_compose_task_runs_in_that_tasks_database_not_the_supervisors(
    tmp_path, ppy_home, supervisor_env
) -> None:
    clone = Path(make_git_repo(tmp_path / "backend"))
    _compose_repo(clone)
    # Dispatched before the stack was declared: nothing was recorded on the task.
    task_id = _task(clone)
    server = SupervisorServer()
    server.start_background()
    lines: list[str] = []
    try:
        code = gate.run_from_cli(
            task_id=task_id,
            repo=None,
            full=False,
            out=lines.append,
            client=SupervisorClient(server.socket_path),
        )
    finally:
        server.stop()

    assert code == 0
    (result,) = _results(task_id)
    output = Path(result["output_path"]).read_text("utf-8")
    port = 6000 + task_id
    assert f"project=task_{task_id} port={port} " in output
    assert f"test_db=postgresql://u@localhost:{port}/test_{task_id} db=" in output
    for shared in ("chat-with-agents", "5433", "lightwork"):
        assert shared not in output
    assert result["compose_project"] == f"task_{task_id}"
    assert result["database"] == f"test_{task_id}"
    assert f"environment: compose project task_{task_id}, database test_{task_id}" in lines
    # Settled the way dispatch would have, so teardown finds the stack.
    assert compose.project_for(task_id) == f"task_{task_id}"


def test_two_tasks_gates_render_distinct_projects_and_databases(tmp_path, ppy_home) -> None:
    clone = Path(make_git_repo(tmp_path / "backend"))
    _compose_repo(clone)
    first, second = _task(clone), _task(clone)

    conn = init_db()
    try:
        row = store.get_repo(conn, "backend")
        rendered = [environment.render_task_env(conn, row, task) for task in (first, second)]
    finally:
        conn.close()

    projects = {values["COMPOSE_PROJECT_NAME"] for values in rendered}
    databases = {environment.database_name(values) for values in rendered}
    ports = {values["PAPAYA_DB_PORT"] for values in rendered}
    assert len(projects) == len(databases) == len(ports) == 2
    assert None not in databases


def test_a_baseline_gate_runs_the_base_commit_in_a_private_scratch_environment(
    tmp_path, ppy_home, supervisor_env, monkeypatch
) -> None:
    clone = Path(make_git_repo(tmp_path / "backend"))
    base = gate.head_of(str(clone))
    (clone / "change.txt").write_text("x\n", "utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "c"], check=True, capture_output=True)
    _compose_repo(clone)
    task_id = _task(clone)
    downs: list[str] = []
    monkeypatch.setattr(compose, "down", lambda project: downs.append(project) or {})
    absent = SupervisorClient(socket_path=str(tmp_path / "nobody.sock"))
    lines: list[str] = []

    code = gate.run_from_cli(
        task_id=task_id, repo=None, full=False, baseline=base[:10], out=lines.append, client=absent
    )

    assert code == 0
    scope = f"gate_base_local_{base[:12]}"
    (result,) = _results(None)
    assert result["baseline"] is True and result["head_sha"] == base
    assert result["compose_project"] == scope and result["database"] == f"test_{scope}"
    output = Path(result["output_path"]).read_text("utf-8")
    assert f"project={scope} " in output and f"head={base}" in output
    for elsewhere in ("chat-with-agents", "5433", "lightwork", f"task_{task_id}"):
        assert elsewhere not in output
    # Recorded, but not as the task's verdict, and nothing of it is left behind.
    assert _results(task_id) == [] and gate.verdict(task_id).state == gate.NONE
    assert "baseline local gate green" in lines[-1]
    assert downs == [scope]
    worktrees = subprocess.run(
        ["git", "-C", str(clone), "worktree", "list"], capture_output=True, text=True, check=True
    ).stdout
    assert scope not in worktrees
    assert not (ppy_home / "gates" / "scratch" / f"backend-{scope}").exists()


def test_two_identical_red_gates_at_one_head_refuse_a_third(tmp_path, ppy_home) -> None:
    clone = Path(make_git_repo(tmp_path / "backend"))
    _compose_repo(clone)
    task_id = _task(clone)
    repos.set_settings(
        "backend",
        local_gate="echo 'FAILED tests/test_a.py::test_b - assert 1 == 2'; "
        "echo '1 failed, 9 passed in 0.01s'; exit 1",
    )

    for _ in range(gate.REPEATED_RED):
        result = gate.run(gate.resolve(task_id=task_id), expected=None, memory=lambda _pid: None)
        assert result.failing_tests == ["tests/test_a.py::test_b"]

    verdict = gate.verdict(task_id)
    assert verdict.state == gate.RED and len(verdict.repeated) == 2
    with pytest.raises(gate.GateError, match="red 2 times") as refused:
        gate.resolve(task_id=task_id)
    assert "--baseline" in str(refused.value)
    assert len(_results(task_id)) == 2
    # A baseline is still allowed: it is how the decision gets made.
    assert gate.resolve(task_id=task_id, baseline="HEAD").baseline


def test_readiness_warns_on_a_compose_repo_whose_gates_can_share_a_database(
    tmp_path, ppy_home, machine
) -> None:
    clone = tmp_path / "backend"
    clone.mkdir()
    _compose_repo(clone)
    repos.set_settings("backend", test_db_url_template="postgresql://u@localhost:{port}/test")

    def warning() -> readiness.Problem | None:
        found = [p for p in readiness.check().problems if p.code == readiness.GATE_ENV_NOT_ISOLATED]
        return found[0] if found else None

    problem = warning()
    assert problem is not None and not problem.blocking
    assert "backend (a database URL template without {task_id})" in problem.summary

    repos.set_settings("backend", test_db_url_template=TEMPLATE)
    assert warning() is None

    # The live shape of #47: a compose file, and no stack declared at all.
    repos.set_settings("backend", compose_stack="no")
    assert warning() is None
    machine.files.add(f"{clone}/docker-compose.yml")
    problem = warning()
    assert problem is not None
    assert "ships a compose file but declares no compose stack" in problem.summary


# --------------------------------------------------------------------------- #
# The repository already answers this: read it, do not ask a person (2026-09-22)
# --------------------------------------------------------------------------- #

POLYWEAVE_COMPOSE = """\
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: polyweave
      POSTGRES_PASSWORD: polyweave
      POSTGRES_DB: polyweave
    ports:
      # 5440 stays clear of the backend stack
      - '${POLYWEAVE_DB_PORT:-5440}:5432'
"""

#: papaya-backend's shape: the compose file publishes the *superuser*, and the
#: database its services actually use is named only in the Makefile.
BACKEND_COMPOSE = """\
services:
  db:
    image: papaya-shared-postgres:pg18-pgvector
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: postgres
    ports:
      - "${PAPAYA_DB_PORT:-5433}:5432"
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
"""

BACKEND_MAKEFILE = """\
PAPAYA_DB_PORT        ?= 5433
BACKEND_DATABASE_URL  ?= postgresql+asyncpg://lightwork:lightwork@localhost:$(PAPAYA_DB_PORT)/lightwork
"""


def test_a_compose_file_answers_the_port_the_variable_and_the_database(tmp_path) -> None:
    clone = tmp_path / "polyweave"
    clone.mkdir()
    (clone / "docker-compose.yml").write_text(POLYWEAVE_COMPOSE)

    derived = environment.derive_isolation(clone)

    assert derived is not None
    assert derived.db_port_base == 5440
    assert derived.db_port_variable == "POLYWEAVE_DB_PORT"
    assert (
        derived.test_db_url_template
        == "postgresql://polyweave:polyweave@localhost:{port}/polyweave_test_{task_id}"
    )
    assert derived.evidence == "repo:docker-compose.yml:10"


def test_the_makefiles_role_beats_the_compose_superuser(tmp_path) -> None:
    """papaya-backend publishes `postgres:postgres` and uses `lightwork` (2026-09-22)."""
    clone = tmp_path / "backend"
    clone.mkdir()
    (clone / "docker-compose.yml").write_text(BACKEND_COMPOSE)
    (clone / "Makefile").write_text(BACKEND_MAKEFILE)

    derived = environment.derive_isolation(clone)

    assert derived is not None
    assert (derived.db_port_base, derived.db_port_variable) == (5433, "PAPAYA_DB_PORT")
    assert (
        derived.test_db_url_template
        == "postgresql://lightwork:lightwork@localhost:{port}/lightwork_test_{task_id}"
    )


@pytest.mark.parametrize(
    ("published", "port", "variable"),
    [
        ('"5433:5432"', 5433, environment.DB_PORT_VARIABLE),
        ('"127.0.0.1:5433:5432"', 5433, environment.DB_PORT_VARIABLE),
        ('"${DB_PORT:-5444}:5432"', 5444, "DB_PORT"),
    ],
)
def test_every_way_a_port_is_published_is_read(tmp_path, published, port, variable) -> None:
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "compose.yaml").write_text(
        "services:\n  db:\n    image: postgres:16\n    ports:\n      - " + published + "\n"
    )
    derived = environment.derive_isolation(clone)
    assert derived is not None
    assert (derived.db_port_base, derived.db_port_variable) == (port, variable)


def test_a_variable_with_no_default_is_taken_from_the_makefile(tmp_path) -> None:
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "compose.yaml").write_text(
        'services:\n  db:\n    image: postgres:16\n    ports:\n      - "${PG_PORT}:5432"\n'
    )
    (clone / "Makefile").write_text("PG_PORT ?= 5455\n")
    derived = environment.derive_isolation(clone)
    assert derived is not None and derived.db_port_base == 5455


@pytest.mark.parametrize(
    "compose",
    [
        "services:\n  web:\n    image: nginx\n    ports:\n      - '8080:80'\n",  # no database
        "services:\n  db:\n    image: postgres:16\n",  # publishes nothing
        "services:\n  db:\n    image: postgres:16\n    ports:\n      - '${PG_PORT}:5432'\n",
        "not: a compose file\n",
        "",
    ],
)
def test_a_repository_that_says_nothing_is_left_alone(tmp_path, compose) -> None:
    """Nothing is invented: a repository with no answer is still the person's to set."""
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "compose.yaml").write_text(compose)
    assert environment.derive_isolation(clone) is None


def test_the_start_remedy_fills_it_in_and_readiness_stops_asking(
    tmp_path, ppy_home, machine
) -> None:
    """End to end: a registered repo with a compose file and blank settings heals itself."""
    clone = tmp_path / "polyweave"
    clone.mkdir()
    _compose_repo(clone)
    repos.set_settings(
        "backend",
        compose_stack="no",
        test_db_url_template="",
        db_port_base="",
        db_port_variable="",
    )
    (clone / "docker-compose.yml").write_text(POLYWEAVE_COMPOSE)
    machine.files.add(f"{clone}/docker-compose.yml")

    def warning() -> readiness.Problem | None:
        found = [p for p in readiness.check().problems if p.code == readiness.GATE_ENV_NOT_ISOLATED]
        return found[0] if found else None

    assert warning() is not None

    (line,) = environment.keep_isolation_right()

    assert "backend: gates are isolated per task" in line
    assert "POLYWEAVE_DB_PORT from 5440" in line
    assert "repo:docker-compose.yml:10" in line
    env = repos.get_settings("backend").environment
    assert env.db_port_base == 5440 and env.db_port_variable == "POLYWEAVE_DB_PORT"
    assert "{task_id}" in (env.test_db_url_template or "")
    assert warning() is None
    assert environment.keep_isolation_right() == []  # nothing left to fill


def test_a_value_a_person_set_is_never_overwritten(tmp_path, ppy_home, machine) -> None:
    clone = tmp_path / "polyweave"
    clone.mkdir()
    _compose_repo(clone)
    (clone / "docker-compose.yml").write_text(POLYWEAVE_COMPOSE)
    machine.files.add(f"{clone}/docker-compose.yml")
    repos.set_settings("backend", compose_stack="no", db_port_base=6000, test_db_url_template="")

    environment.keep_isolation_right()

    env = repos.get_settings("backend").environment
    assert env.db_port_base == 6000  # theirs
    assert "{task_id}" in (env.test_db_url_template or "")  # the blank was filled
