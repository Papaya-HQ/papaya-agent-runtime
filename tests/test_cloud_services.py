"""A cloud runner with no Docker works compose repositories anyway, leaving their
service-backed checks to CI (PAP-334).

On a person's machine "Docker is not running" is theirs to fix and holds the
ticket; on a cloud runner nobody can start Docker, so the work goes ahead without
the services and says what it left.
"""

from __future__ import annotations

import pytest

from papaya_agent_runtime import compose, environment, readiness

CLOUD = {"PPY_CLOUD_RUNNER": "1"}


# ── is this a cloud runner without Docker? ─────────────────────────────────


def test_a_persons_machine_never_has_its_services_waived(monkeypatch) -> None:
    monkeypatch.setattr(compose, "docker_bin", lambda: None)

    assert compose.services_unavailable({}) is False


def test_a_cloud_runner_without_docker_has_no_services(monkeypatch) -> None:
    monkeypatch.setattr(compose, "docker_bin", lambda: None)

    assert compose.services_unavailable(CLOUD) is True


def test_a_cloud_runner_whose_docker_answers_keeps_its_services(monkeypatch) -> None:
    class _Done:
        returncode = 0

    monkeypatch.setattr(compose, "docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(compose, "_run", lambda argv: _Done())

    assert compose.services_unavailable(CLOUD) is False


# ── readiness: a fact, not a blocker ───────────────────────────────────────


@pytest.fixture
def compose_repo(machine, tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / "ppy"))
    machine.missing.add("docker")
    machine.files.add("/repos/backend/docker-compose.yml")
    return [{"name": "backend", "local_path": "/repos/backend"}]


def _docker_problem(registered) -> readiness.Problem:
    problems: list[readiness.Problem] = []
    readiness._toolchain_problems(problems, registered)
    (problem,) = [p for p in problems if p.code == readiness.DOCKER_NOT_RUNNING]
    return problem


def test_on_a_persons_machine_no_docker_holds_that_repositorys_tickets(
    compose_repo, monkeypatch
) -> None:
    monkeypatch.delenv("PPY_CLOUD_RUNNER", raising=False)

    problem = _docker_problem(compose_repo)
    verdict = readiness.Readiness(state=readiness.READY, problems=[problem])

    assert problem.steps
    assert readiness.setup_blocker(verdict, "backend") is problem


def test_on_a_cloud_runner_no_docker_is_said_and_holds_nothing(compose_repo, monkeypatch) -> None:
    monkeypatch.setenv("PPY_CLOUD_RUNNER", "1")

    problem = _docker_problem(compose_repo)
    verdict = readiness.Readiness(state=readiness.READY, problems=[problem])

    assert problem.steps == ()
    assert problem.repos == ("backend",)
    assert "left to CI" in problem.summary
    assert readiness.setup_blocker(verdict, "backend") is None


# ── the worker's environment block ─────────────────────────────────────────


def _row(tmp_path, **settings) -> dict:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    return {"name": "backend", "local_path": str(tmp_path), **settings}


def test_without_services_the_block_says_what_to_run_and_what_to_leave() -> None:
    text = environment.render(
        environment.RepoEnvironment(
            repo="backend",
            compose_stack="yes",
            full_suite_command="make verify",
            full_suite_owner="supervisor",
        ),
        task_id=7,
        evidence_path="/wt/7/.ppy-evidence",
        services_missing=True,
    )

    assert environment.NO_SERVICES_RULE in text
    assert "not run here: needs Docker services; left to CI" in text
    # The suite the supervisor would have run is CI's here, and there is no stack.
    assert "CI runs it on your pull request" in text
    assert "the supervisor runs it once" not in text
    assert "compose project" not in text


def test_with_services_the_block_is_unchanged() -> None:
    text = environment.render(
        environment.RepoEnvironment(repo="backend"),
        task_id=7,
        evidence_path="/wt/7/.ppy-evidence",
    )

    assert "No Docker on this runner" not in text


def test_a_compose_repository_on_a_cloud_runner_has_no_supervisor_full_suite(
    tmp_path, monkeypatch
) -> None:
    row = _row(tmp_path, full_suite_command="make verify", full_suite_owner="supervisor")
    monkeypatch.setattr(compose, "services_unavailable", lambda environ=None: True)

    assert environment.services_missing(row) is True
    assert environment.full_suite_is_supervisors(row) is False


def test_the_same_repository_on_a_persons_machine_keeps_its_supervisor_full_suite(
    tmp_path, monkeypatch
) -> None:
    row = _row(tmp_path, full_suite_command="make verify", full_suite_owner="supervisor")
    monkeypatch.setattr(compose, "services_unavailable", lambda environ=None: False)

    assert environment.services_missing(row) is False
    assert environment.full_suite_is_supervisors(row) is True


def test_a_repository_without_compose_is_never_missing_services(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(compose, "services_unavailable", lambda environ=None: True)

    assert environment.services_missing({"name": "web", "local_path": str(tmp_path)}) is False
