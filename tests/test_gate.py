"""A gate longer than a harness tool call is never run inside one (PAP-213).

On 2026-09-16 a worker ran the backend suite as a tool call. The harness capped the
call at ten minutes and moved it to the background, the worker ended its turn to wait,
and the session's end killed the suite. Two things made that inevitable: no
repository the runtime registered itself had a gate policy (so the worker was told
"local gate: not set"), and nothing could run a long gate except a tool call.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import make_git_repo
from papaya_agent_runtime import cli, environment, gate, readiness, repos, solicit
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer

# ── The policy is read from the repository ──────────────────────────────────


def _register(path: Path, name: str = "app") -> str:
    conn = init_db()
    store.add_repo(
        conn,
        name=name,
        origin=f"https://github.com/acme/{name}.git",
        local_path=str(path),
        default_branch="main",
        base_sha="a" * 40,
        forge_url=f"https://github.com/acme/{name}",
    )
    conn.close()
    return name


def _stored(name: str = "app") -> environment.RepoEnvironment:
    conn = init_db()
    try:
        return environment.for_repo(store.get_repo(conn, name))
    finally:
        conn.close()


def test_a_pre_push_hook_that_runs_the_suite_sets_the_hook_flag(tmp_path, ppy_home) -> None:
    clone = Path(make_git_repo(tmp_path / "backend"))
    hook = clone / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nset -e\nmake verify\n", encoding="utf-8")
    hook.chmod(0o755)
    _register(clone, "backend")

    report, notes = solicit.onboard("backend")

    stored = _stored("backend")
    assert stored.push_hook_runs_full_suite is True
    assert report.gate is not None and report.gate.push_hook_runs_full_suite
    assert "runs `make verify`" in " ".join(report.gate.evidence)
    assert "pre-push hook runs the full suite: yes" in notes.read_text(encoding="utf-8")
    # And the environment block a worker is handed now says so.
    block = environment.render(stored, task_id=4, evidence_path="/wt/.ppy-evidence")
    assert "pre-push hook runs the full suite, so do not push" in block


def test_a_hook_that_only_lints_is_not_a_full_suite(tmp_path, ppy_home) -> None:
    clone = Path(make_git_repo(tmp_path / "web"))
    (clone / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\nruff check .\n", "utf-8")
    _register(clone)
    solicit.onboard("app")
    assert _stored().push_hook_runs_full_suite is False


def _verify_makefile(clone: Path) -> None:
    (clone / "Makefile").write_text(
        ".PHONY: lint-check verify\nlint-check:\n\truff check .\nverify: lint-check\n\tpytest\n",
        encoding="utf-8",
    )


def test_the_gates_are_the_ones_the_repositorys_agents_md_declares(tmp_path, ppy_home) -> None:
    """Shane, 2026-09-17: the repository owns its gates; the runtime quotes them."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _verify_makefile(clone)
    (clone / "AGENTS.md").write_text(
        "# Agents\n\n## Testing\n\nRun make lint-check while working, make verify before a PR.\n",
        encoding="utf-8",
    )
    workflows = clone / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n      - run: make verify\n",
        "utf-8",
    )
    _register(clone)

    report, notes = solicit.onboard("app")

    stored = _stored()
    assert stored.local_gate == "make lint-check"
    assert stored.local_gate_source == "AGENTS.md:5"
    assert stored.full_suite_command == "make verify"
    assert stored.full_suite_command_source == "AGENTS.md:5"
    assert (stored.full_suite_owner, stored.full_suite_owner_source) == (
        "ci",
        ".github/workflows/ci.yml:5",
    )
    assert stored.supervisor_runs_full_suite is False
    # The notes quote the repository's own words, with where they are.
    text = notes.read_text(encoding="utf-8")
    assert "AGENTS.md:5: “Run make lint-check while working, make verify before a PR.”" in text
    assert report.gate is not None and report.gate.unknown == []


def test_roles_come_from_lead_ins_headings_and_fenced_comments(tmp_path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "CONTRIBUTING.md").write_text(
        "## Before opening a pull request\n\n```sh\nuv run pytest -q\n```\n\n"
        "While you work, run:\n\n- `uv run ruff check .`\n- `uv run pytest tests/unit`\n\n"
        "`make docs` builds the docs.\n",
        encoding="utf-8",
    )

    found = solicit.declarations(clone)

    assert [(d.role, d.command, d.source) for d in found] == [
        (solicit.FULL, "uv run pytest -q", "CONTRIBUTING.md:4"),
        (solicit.SCOPED, "uv run ruff check .", "CONTRIBUTING.md:9"),
        (solicit.SCOPED, "uv run pytest tests/unit", "CONTRIBUTING.md:10"),
    ]
    policy = solicit.derive_gate_policy(clone)
    assert policy.local_gate == "uv run ruff check ."
    # No workflow runs it: the supervisor runs it once at the delivered head.
    assert policy.full_suite_owner == "supervisor"


def test_a_repo_with_targets_but_no_instructions_is_unknown_not_guessed(tmp_path, ppy_home) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _verify_makefile(clone)
    (clone / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "test:unit": "vitest run unit"}}), "utf-8"
    )
    _register(clone)

    report, notes = solicit.onboard("app")

    stored = _stored()
    assert (stored.local_gate, stored.full_suite_command) == (None, None)
    assert report.gate is not None and report.gate.unknown == ["scoped gate", "full suite"]
    assert "Unknown: scoped gate, full suite" in notes.read_text(encoding="utf-8")


def test_onboarding_never_overwrites_a_set_gate_but_an_explicit_one_wins(
    tmp_path, ppy_home
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "AGENTS.md").write_text("Run `make test` while working.\n", "utf-8")
    _register(clone)
    repos.set_settings("app", local_gate="uv run pytest tests/radar -q")
    assert _stored().local_gate_source == "person"

    solicit.onboard("app")
    assert _stored().local_gate == "uv run pytest tests/radar -q"
    assert _stored().local_gate_source == "person"

    assert cli.main(["repo", "onboard", "app", "--local-gate", "make lint"]) == 0
    assert (_stored().local_gate, _stored().local_gate_source) == ("make lint", "person")


def test_ensure_fills_the_policy_of_a_repo_onboarded_before_policies_existed(
    tmp_path, ppy_home, monkeypatch
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "AGENTS.md").write_text("Before a PR, run `make test`.\n", "utf-8")
    _register(clone)
    monkeypatch.setattr(solicit, "_is_onboarded", lambda name: True)

    result = solicit.ensure("app")

    assert not result.registered and not result.onboarded
    assert (_stored().full_suite_command, _stored().full_suite_command_source) == (
        "make test",
        "AGENTS.md:1",
    )


def test_a_repo_that_declares_no_gates_is_a_blocker_asking_its_owner(
    tmp_path, ppy_home, monkeypatch
) -> None:
    for check in ("_harness_problems", "_papaya_problems", "_config_problems", "_client_problems"):
        monkeypatch.setattr(readiness, check, lambda problems: None)
    clone = tmp_path / "clone"
    clone.mkdir()
    _verify_makefile(clone)  # targets to guess from, and no instructions naming them
    _register(clone)
    solicit.onboard("app")
    assert _stored().local_gate is None

    verdict = readiness.check()

    assert [p.code for p in verdict.problems] == ["repo_without_gate_policy"]
    problem = verdict.problems[0]
    assert problem.blocking is False
    assert problem.owner == readiness.USER
    assert problem.steps, "a problem with steps is a blocker reported to the owner"
    question = (
        "Which command is app's quick gate (lint, type check and the touched tests, run before "
        "handing work back); and which command is app's full suite (run once before a pull "
        "request)?"
    )
    assert problem.steps[0] == question
    assert question in problem.summary
    assert "--full-suite-command" in problem.fix
    assert verdict.state == readiness.DEGRADED

    repos.set_settings("app", local_gate="make lint-check")
    assert "full suite (run once" in readiness.check().problems[0].summary
    repos.set_settings("app", full_suite_command="make verify")
    assert readiness.check().problems == []


def test_the_start_remedy_drops_guessed_gates_and_keeps_a_persons(
    tmp_path, ppy_home, caplog
) -> None:
    """A policy the old heuristics stored falls back to what the repository declares."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _verify_makefile(clone)
    (clone / "Makefile").write_text(
        (clone / "Makefile").read_text("utf-8") + "test:\n\tpytest\n", "utf-8"
    )
    (clone / "AGENTS.md").write_text("Before a PR, run `make verify`.\n", "utf-8")
    _register(clone)
    other = tmp_path / "other"
    other.mkdir()
    _verify_makefile(other)
    _register(other, "other")
    conn = init_db()
    # What the old derivation wrote, before sources existed: `make test` / `make verify`
    # / the supervisor owner for app; the same guess, and a person's gate, for other.
    store.update_repo_fields(
        conn,
        "app",
        local_gate="make test",
        full_suite_command="make verify",
        full_suite_owner="supervisor (`ppy gate run --full`)",
    )
    store.update_repo_fields(conn, "other", local_gate="make lint-check", full_suite_owner="ci")
    conn.close()

    with caplog.at_level("INFO", logger="papaya_agent_runtime.solicit"):
        lines = solicit.clear_heuristic_gate_policies()

    app = _stored()
    assert app.local_gate is None  # guessed, and AGENTS.md names no scoped gate
    assert (app.full_suite_command, app.full_suite_command_source) == (
        "make verify",
        "AGENTS.md:1",
    )
    assert (app.full_suite_owner, app.full_suite_owner_source) == (
        "supervisor",
        "no CI workflow runs it",
    )
    kept = _stored("other")
    assert (kept.local_gate, kept.local_gate_source) == ("make lint-check", "person")
    # Not what the old derivation gives for this clone (it would have said supervisor).
    assert (kept.full_suite_owner, kept.full_suite_owner_source) == ("ci", "person")
    assert len(lines) == 1
    assert lines[0].startswith("app: cleared guessed gate answers (local_gate `make test`")
    assert "still unknown: scoped gate" in lines[0]
    assert any("app: cleared guessed gate answers" in r.getMessage() for r in caplog.records)
    # Said once: nothing left to clear on the next start.
    assert solicit.clear_heuristic_gate_policies() == []


def test_readiness_does_not_warn_twice_for_a_repo_nobody_onboarded(ppy_home, monkeypatch) -> None:
    for check in ("_harness_problems", "_papaya_problems", "_config_problems", "_client_problems"):
        monkeypatch.setattr(readiness, check, lambda problems: None)
    _register(Path("/l"))
    assert [p.code for p in readiness.check().problems] == ["repo_not_onboarded"]


# ── Running a gate ──────────────────────────────────────────────────────────


@pytest.fixture
def task_worktree(tmp_path, ppy_home):
    """A task whose worktree is a real git checkout, in a repo with a local gate."""
    worktree = Path(make_git_repo(tmp_path / "worktree"))
    conn = init_db()
    repo_id = store.add_repo(
        conn,
        name="app",
        origin="https://github.com/acme/app.git",
        local_path=str(worktree),
        default_branch="main",
        base_sha="a" * 40,
    )
    run_id = store.create_run(conn, "gate")
    task_id = store.add_task(conn, run_id=run_id, title="gate", repo_id=repo_id)
    store.update_task_fields(conn, task_id, worktree_path=str(worktree), status="in_progress")
    conn.close()
    repos.set_settings("app", local_gate="make test", full_suite_command="make verify")
    return task_id, worktree


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ThreeMinuteSuite:
    """A gate process that prints its summary and exits after three fake minutes."""

    def __init__(self, clock: FakeClock, argv, *, stdout, **_kwargs) -> None:
        self.clock = clock
        self.argv = argv
        self.pid = os.getpid()
        self.returncode = None
        stdout.write("collected 900 items\n")
        stdout.write("============ 900 passed in 180.00s ============\n")
        stdout.flush()

    def poll(self):
        if self.clock.now >= 180:
            self.returncode = 0
        return self.returncode


def _events(task_id: int, kind: str) -> list[dict]:
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
    finally:
        conn.close()


def test_a_three_minute_gate_prints_progress_every_minute_and_records_its_result(
    task_worktree,
) -> None:
    task_id, worktree = task_worktree
    clock = FakeClock()
    lines: list[str] = []
    started: list[ThreeMinuteSuite] = []

    def popen(argv, **kwargs):
        started.append(ThreeMinuteSuite(clock, argv, **kwargs))
        return started[-1]

    spec = gate.resolve(task_id=task_id)
    result = gate.run(
        spec, on_progress=lines.append, clock=clock, sleep=clock.sleep, popen=popen, poll=10
    )

    assert started[0].argv == ["/bin/sh", "-c", "make test"]
    assert [line.split(";")[0] for line in lines] == [
        "local gate still running: 1m00s elapsed",
        "local gate still running: 2m00s elapsed",
        "local gate still running: 3m00s elapsed",
    ]
    assert "last output: ============ 900 passed in 180.00s" in lines[0]
    assert result.green and result.duration_seconds == 180
    assert result.summary == "900 passed in 180.00s"
    head = gate.head_of(str(worktree))
    assert result.head_sha == head

    (recorded,) = _events(task_id, gate.GATE_RESULT)
    assert recorded["command"] == "make test"
    assert recorded["exit_code"] == 0
    assert recorded["duration_seconds"] == 180
    assert recorded["summary"] == "900 passed in 180.00s"
    assert recorded["head_sha"] == head
    assert Path(recorded["output_path"]).parent == worktree / ".ppy-evidence"
    assert "exit=0" in (worktree / ".ppy-evidence" / "receipts.txt").read_text("utf-8")
    assert _events(task_id, gate.GATE_STARTED)


def test_the_worker_environment_block_names_ppy_gate_run(task_worktree) -> None:
    from papaya_agent_runtime import prompts

    task_id, worktree = task_worktree
    text = environment.render(_stored(), task_id=task_id, evidence_path=f"{worktree}/.ppy-evidence")
    assert f"`ppy gate run --task {task_id}`" in text
    assert " ".join(prompts.TEN_MINUTE_RULE.split()) in " ".join(text.split())


def test_the_verdict_is_the_newest_result_at_the_current_head(task_worktree) -> None:
    task_id, worktree = task_worktree
    assert gate.verdict(task_id).state == gate.NONE

    def recorded(exit_code: int) -> None:
        spec = gate.resolve(task_id=task_id)
        clock = FakeClock()

        class Exits:
            pid = os.getpid()
            returncode = exit_code

            def __init__(self, _argv, *, stdout, **_kwargs) -> None:
                stdout.write(f"1 {'passed' if exit_code == 0 else 'failed'} in 0.1s\n")

            def poll(self):
                return self.returncode

        gate.run(spec, clock=clock, sleep=clock.sleep, popen=Exits)

    recorded(1)
    red = gate.verdict(task_id)
    assert red.state == gate.RED and red.result.summary == "1 failed in 0.1s"
    recorded(0)
    assert gate.verdict(task_id).state == gate.GREEN

    # A new commit is a new head: nothing is recorded there yet.
    (worktree / "change.txt").write_text("x\n", "utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-qm", "change"], check=True, capture_output=True
    )
    assert gate.verdict(task_id).state == gate.NONE


def test_the_full_suite_runs_once_per_head_and_only_when_it_is_the_supervisors(
    task_worktree,
) -> None:
    task_id, _worktree = task_worktree
    runs: list[dict] = []

    def run(**kwargs) -> int:
        runs.append(kwargs)
        spec = gate.resolve(task_id=task_id, full=True)
        clock = FakeClock()

        class Green:
            pid = os.getpid()
            returncode = 0

            def __init__(self, _argv, *, stdout, **_kwargs) -> None:
                stdout.write("12 passed in 0.1s\n")

            def poll(self):
                return self.returncode

        gate.run(spec, clock=clock, sleep=clock.sleep, popen=Green, expected=None)
        return 0

    # CI owns it (the default): the runtime runs nothing.
    assert gate.full_suite_once(task_id, run=run) is None
    repos.set_settings("app", full_suite_owner="supervisor")

    first = gate.full_suite_once(task_id, run=run)
    again = gate.full_suite_once(task_id, run=run)

    assert len(runs) == 1 and runs[0]["full"] is True
    assert first is not None and first.state == gate.GREEN
    assert first.result.command == "make verify" and first.result.full
    assert again is not None and again.result == first.result
    # A scoped gate result at the same head is not a full-suite record.
    assert gate.verdict(task_id, full=False).state == gate.NONE


def test_a_full_suite_tool_call_is_refused_and_steered_as_policy(
    task_worktree, monkeypatch
) -> None:
    from papaya_agent_runtime import tool_learning
    from papaya_agent_runtime.providers.base import TaskSpec
    from papaya_agent_runtime.providers.claude import ClaudeAdapter
    from papaya_agent_runtime.providers.command_rules import FULL_SUITE_REFUSAL

    task_id, worktree = task_worktree
    conn = init_db()
    row = store.get_repo(conn, "app")
    conn.close()
    denied = environment.denied_tools(row)
    assert denied == ["Bash(make verify)"]
    spec = TaskSpec(task_id, "t", "brief", str(worktree), "", "claude", denied_tools=denied)
    argv = ClaudeAdapter().start(spec)
    assert argv[argv.index("--disallowedTools") + 1] == "Bash(make verify)"

    steers: list[str] = []
    monkeypatch.setattr(
        tool_learning, "steer_worker", lambda _task, message: steers.append(message)
    )
    tool_learning.learn(
        [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "make verify"}}],
        task_id=task_id,
        run_id=None,
        worktree=str(worktree),
    )

    (payload,) = _events(task_id, tool_learning.PERMISSION_DENIED)
    assert payload["kind"] == tool_learning.POLICY_REFUSAL
    assert payload["in_family"] is False
    (message,) = steers
    assert FULL_SUITE_REFUSAL in message


@pytest.mark.parametrize(
    ("output", "summary"),
    [
        (
            "collecting\n=== 3 failed, 897 passed in 700.10s ===\n",
            "3 failed, 897 passed in 700.10s",
        ),
        (
            "running 4 tests\ntest result: ok. 4 passed; 0 failed\n",
            "test result: ok. 4 passed; 0 failed",
        ),
        ("building\nmake: *** [test] Error 2\n", "make: *** [test] Error 2"),
        ("", ""),
    ],
)
def test_the_summary_line_is_the_one_that_says_how_it_went(output: str, summary: str) -> None:
    assert gate.summary_line(output) == summary


def test_a_repo_with_no_gate_recorded_is_refused_with_how_to_set_one(task_worktree, capsys) -> None:
    task_id, _worktree = task_worktree
    repos.set_settings("app", local_gate="")
    assert cli.main(["gate", "run", "--task", str(task_id)]) == 2
    err = capsys.readouterr().err
    assert "app has no local gate recorded" in err
    assert "ppy repo onboard app" in err


def test_without_a_supervisor_the_gate_runs_in_the_calling_process(task_worktree, tmp_path) -> None:
    task_id, _worktree = task_worktree
    repos.set_settings("app", local_gate="echo '1 failed in 0.01s'; exit 1")
    lines: list[str] = []
    absent = SupervisorClient(socket_path=str(tmp_path / "nobody.sock"))

    code = gate.run_from_cli(
        task_id=task_id, repo=None, full=False, out=lines.append, client=absent
    )

    assert code == 1
    assert "no supervisor is running" in lines[0]
    assert "red (exit 1)" in lines[-1] and "1 failed in 0.01s" in lines[-1]
    assert _events(task_id, gate.GATE_RESULT)[-1]["exit_code"] == 1


@pytest.fixture
def supervisor(ppy_home):
    server = SupervisorServer()
    server.start_background()
    try:
        yield SupervisorClient(server.socket_path)
    finally:
        server.stop()


def test_the_supervisor_runs_the_gate_and_a_caller_that_ran_out_of_time_attaches_again(
    task_worktree, supervisor
) -> None:
    task_id, _worktree = task_worktree
    repos.set_settings("app", full_suite_command="sleep 1; echo '12 passed in 1.00s'")
    first: list[str] = []

    # A caller whose budget is gone hands back "still running", and the gate goes on.
    code = gate.run_from_cli(
        task_id=task_id, repo=None, full=True, wait_seconds=0, out=first.append, client=supervisor
    )
    assert code == gate.STILL_RUNNING
    assert "started the full suite" in first[0]
    assert "Run this same command again" in first[-1]

    second: list[str] = []
    code = gate.run_from_cli(
        task_id=task_id, repo=None, full=True, out=second.append, client=supervisor
    )
    assert code == 0
    assert "attached to the full suite" in second[0]
    assert "full suite green" in second[-2] and "12 passed in 1.00s" in second[-2]
    (recorded,) = _events(task_id, gate.GATE_RESULT)
    assert recorded["full"] is True and recorded["exit_code"] == 0
    assert len(_events(task_id, gate.GATE_STARTED)) == 1, "the second call started another gate"


def test_ppy_gate_run_through_the_cli_exits_with_the_gates_verdict(
    task_worktree, supervisor, capsys
) -> None:
    task_id, _worktree = task_worktree
    repos.set_settings("app", local_gate="echo '2 passed in 0.01s'")
    assert cli.main(["gate", "run", "app", "--task", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "local gate green" in out and "2 passed in 0.01s" in out
    assert cli.main(["gate", "run", "other", "--task", str(task_id)]) == 2
    assert "task" in capsys.readouterr().err
