"""The heartbeat: one relayable line of team state per tick, silent only when idle."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from papaya_agent_runtime import board, cli, compose, delivery, watch
from papaya_agent_runtime.config import MMConfig, save_config
from papaya_agent_runtime.state import init_db, store


@pytest.fixture(autouse=True)
def no_real_forge(monkeypatch):
    """No test in this file may shell out: the forge is faked at its one seam.

    The default answer stands in for a machine with no ``gh`` at all, which is
    also the honest answer for every test that is not about pull requests.
    """
    monkeypatch.setattr(watch, "_run_gh", lambda args, cwd=None: (127, ""))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return init_db()


def delivered_task(conn, tmp_path, branch: str, *, status: str = "delivered") -> int:
    """A finished task whose work sits on a branch, with a worktree to run gh from."""
    run_id = store.create_run(conn, "ship the thing")
    task_id = store.add_task(conn, run_id=run_id, title=f"work on {branch}")
    worktree = tmp_path / f"wt-{branch.replace('/', '-')}"
    worktree.mkdir(parents=True, exist_ok=True)
    store.update_task_fields(conn, task_id, branch=branch, worktree_path=str(worktree))
    store.set_task_status(conn, task_id, status)
    return task_id


def fake_forge(monkeypatch, *, pr: dict | None, checks: list[dict] | None = None):
    """Answer `gh pr list` / `gh pr checks` from canned rows; record every call made."""
    calls: list[list[str]] = []

    def run_gh(args, cwd=None):
        calls.append(list(args))
        if args[1] == "list":
            return (0, json.dumps([pr] if pr else []))
        # gh exits non-zero whenever a check is failing or still running.
        bad = any(c.get("bucket") in ("fail", "pending") for c in checks or [])
        return (1 if bad else 0, json.dumps(checks or []))

    monkeypatch.setattr(watch, "_run_gh", run_gh)
    return calls


OPEN_PR = {
    "number": 779,
    "state": "OPEN",
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
    "baseRefName": "main",
}


def test_tick_reports_owed_tasks_new_events_and_the_ledger(home) -> None:
    conn = home
    run_id = store.create_run(conn, "ship the thing")
    done = store.add_task(conn, run_id=run_id, title="finished, awaiting review")
    store.set_task_status(conn, done, "worker_done")
    board.add("review task", task_id=done)

    baseline = watch.max_event_id(conn)
    store.append_event(conn, kind="worker_done", payload={}, run_id=run_id, task_id=done)
    store.append_event(conn, kind="worker_item.completed", payload={}, run_id=run_id, task_id=done)
    store.append_event(conn, kind="error", payload={"summary": "x"}, run_id=run_id, task_id=done)

    snap = watch.tick(conn, since_event_id=baseline)
    assert [r["id"] for r in snap["needs_me"]] == [done]
    assert snap["new_events"] == {"worker_done": 1, "error": 1}  # provider chatter excluded
    assert snap["open_todos"] == 1
    assert snap["last_event_id"] > baseline

    line = watch.render(snap)
    assert "no workers in flight" in line
    assert f"needs me: t{done} worker_done" in line
    assert "new since last tick: error×1, worker_done×1" in line
    assert "open todos: 1" in line

    # A second tick from the new cursor reports nothing new — but still reports.
    again = watch.tick(conn, since_event_id=snap["last_event_id"])
    assert again["new_events"] == {}
    assert "new since last tick: none" in watch.render(again)


def test_run_once_prints_exactly_one_line_and_returns(home) -> None:
    out = io.StringIO()
    slept: list[float] = []
    rc = watch.run(interval=1, once=True, out=out, sleep=slept.append)
    assert rc == 0
    assert out.getvalue().count("\n") == 1
    assert out.getvalue().startswith("TEAM ")
    assert slept == []


def test_watch_prints_usage_advisories(home) -> None:
    conn = home
    cfg = MMConfig()
    cfg.usage.input_ceiling_per_task = 10
    save_config(cfg)
    run_id = store.create_run(conn, "usage")
    task_id = store.add_task(conn, run_id=run_id, title="expensive")
    store.record_usage(
        conn,
        run_id=run_id,
        task_id=task_id,
        provider="codex",
        model="m",
        reasoning="low",
        input_tokens=11,
        output_tokens=1,
    )
    snapshot = watch.tick(conn)
    assert snapshot["usage_advisories"][0]["task_id"] == task_id
    assert "usage advisory" in watch.render(snapshot)


def test_run_loops_on_the_interval_until_stopped(home) -> None:
    conn = home
    # Something is waiting on the manager, so every tick has news to report; the
    # idle case is its own test, below.
    store.set_task_status(
        conn,
        store.add_task(conn, run_id=store.create_run(conn, "ship it"), title="review me"),
        "worker_done",
    )
    out = io.StringIO()
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(interval=300, out=out, sleep=fake_sleep)
    assert calls == [300, 300, 300]
    assert out.getvalue().count("TEAM ") == 3


def test_cli_watch_once(home, capsys) -> None:
    assert cli.main(["watch", "--once"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("TEAM ") and "no workers in flight" in line
    assert cli.main(["watch", "--once", "--json"]) == 0
    assert '"in_flight"' in capsys.readouterr().out


def test_the_line_carries_a_green_pull_request_with_its_base(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/green")
    fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pass"}])

    snap = watch.tick(conn)
    assert snap["prs"][0]["pr"] == 779
    assert snap["prs"][0]["ci"] == "pass"
    assert "prs: PR #779 -> main: ci pass, mergeable" in watch.render(snap)


def test_a_failing_check_is_named_so_the_manager_knows_what_broke(
    home, tmp_path, monkeypatch
) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/red")
    fake_forge(
        monkeypatch,
        pr={**OPEN_PR, "number": 555},
        checks=[{"name": "Unit tests", "bucket": "fail"}, {"name": "Lint", "bucket": "pass"}],
    )

    snap = watch.tick(conn)
    assert snap["prs"][0]["ci"] == "fail"
    assert "prs: PR #555 -> main: ci fail (Unit tests), mergeable" in watch.render(snap)


def test_checks_still_running_read_as_pending(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/running")
    fake_forge(
        monkeypatch,
        pr=OPEN_PR,
        checks=[{"name": "Unit tests", "bucket": "pending"}, {"name": "Lint", "bucket": "pass"}],
    )

    assert "prs: PR #779 -> main: ci pending, mergeable" in watch.render(watch.tick(conn))


def test_a_branch_behind_its_base_says_so(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/stale")
    fake_forge(
        monkeypatch,
        pr={**OPEN_PR, "mergeStateStatus": "BEHIND"},
        checks=[{"name": "Unit tests", "bucket": "pass"}],
    )

    assert "PR #779 -> main: ci pass, behind base" in watch.render(watch.tick(conn))


def test_without_gh_the_tick_says_unknown_rather_than_failing(home, tmp_path) -> None:
    conn = home
    task_id = delivered_task(conn, tmp_path, "harness/no-gh")  # autouse fixture: gh is absent

    snap = watch.tick(conn)
    assert snap["prs"][0] == {
        "known": False,
        "pr": None,
        "ci": "unknown",
        "failing": [],
        "task_id": task_id,
        "branch": "harness/no-gh",
        "status": "delivered",
    }
    assert "ci: unknown" not in watch.render(snap)
    assert "prs:" not in watch.render(snap)


def test_a_branch_with_no_pull_request_yet_adds_no_noise(home, tmp_path, monkeypatch) -> None:
    conn = home
    task_id = delivered_task(conn, tmp_path, "harness/unopened", status="worker_done")
    fake_forge(monkeypatch, pr=None)

    line = watch.render(watch.tick(conn))
    assert "prs:" not in line
    assert f"needs me: t{task_id} worker_done" in line


def test_a_merged_pull_request_is_news_once_and_then_never_asked_about_again(
    home, tmp_path, monkeypatch
) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/landing")
    fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pass"}])

    first = watch.tick(conn)
    assert "prs: PR #779 -> main: ci pass, mergeable" in watch.render(first)
    settled = watch.settled_index(None, first["prs"])
    assert settled == {}  # an open pull request has not stopped moving

    calls = fake_forge(monkeypatch, pr={**OPEN_PR, "state": "MERGED"})
    second = watch.tick(conn, previous_prs=watch.pr_index(first["prs"]), settled_prs=settled)
    line = watch.render(second)
    assert "changed: PR #779 open -> merged" in line
    assert "prs:" not in line  # the flip is the news; it earns no standing segment
    assert [c[1] for c in calls] == ["list"]  # nothing left to ask the checks about
    settled = watch.settled_index(settled, second["prs"])

    calls.clear()
    third = watch.tick(conn, previous_prs=watch.pr_index(second["prs"]), settled_prs=settled)
    assert calls == []  # a terminal state cannot change: stop paying for it
    assert third["pr_changes"] == []
    assert "prs:" not in watch.render(third)


def test_watch_records_the_forge_merge_commit_tears_down_and_retires_the_pr(
    home, tmp_path, monkeypatch
) -> None:
    conn = home
    task_id = delivered_task(conn, tmp_path, "harness/landing")
    compose.record_project(task_id, f"task_{task_id}", conn=conn)
    monkeypatch.setattr(
        compose,
        "down",
        lambda project: {
            "project": project,
            "ran": True,
            "ok": True,
            "removed": ["network"],
            "detail": "removed",
        },
    )
    merge_sha = "a" * 40
    calls = fake_forge(
        monkeypatch,
        pr={
            **OPEN_PR,
            "state": "MERGED",
            "mergedAt": "2026-09-11T18:00:00Z",
            "mergeCommit": {"oid": merge_sha},
        },
    )

    first = watch.tick(conn)
    assert store.get_task(conn, task_id)["merged_sha"] == merge_sha
    assert first["recorded_merges"][0]["merge_commit"] == merge_sha
    assert "recorded merged" in watch.render(first)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id = ? AND kind = 'compose_down'", (task_id,)
        ).fetchone()[0]
        == 1
    )

    calls.clear()
    second = watch.tick(conn)
    assert second["prs"] == []
    assert calls == []


def test_closed_unmerged_and_non_boolean_merge_payloads_are_never_recorded(
    home, tmp_path, monkeypatch
) -> None:
    conn = home
    closed = delivered_task(conn, tmp_path, "harness/closed")
    fake_forge(monkeypatch, pr={**OPEN_PR, "state": "CLOSED"})
    assert watch.tick(conn)["recorded_merges"] == []
    assert store.get_task(conn, closed)["merged_sha"] is None

    malformed = delivered_task(conn, tmp_path, "harness/malformed")
    monkeypatch.setattr(
        watch,
        "_lookup_pr",
        lambda branch, cwd: {
            "known": True,
            "pr": 99,
            "state": "MERGED",
            "merged": "true",
            "merge_commit": "b" * 40,
            "ci": "none",
            "failing": [],
        },
    )
    assert watch.tick(conn)["recorded_merges"] == []
    assert store.get_task(conn, malformed)["merged_sha"] is None


def test_record_merged_is_idempotent_for_a_racing_second_writer(
    home, tmp_path, monkeypatch
) -> None:
    conn = home
    task_id = delivered_task(conn, tmp_path, "harness/race")
    compose.record_project(task_id, f"task_{task_id}", conn=conn)
    monkeypatch.setattr(
        compose,
        "down",
        lambda project: {
            "project": project,
            "ran": True,
            "ok": True,
            "removed": [],
            "detail": "gone",
        },
    )
    sha = "c" * 40
    assert "recorded as delivered" in delivery.record_merged(task_id, sha).note
    assert "already recorded" in delivery.record_merged(task_id, sha).note
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id = ? AND kind = 'delivered'", (task_id,)
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id = ? AND kind = 'compose_down'", (task_id,)
        ).fetchone()[0]
        == 1
    )


def test_run_stops_polling_a_pull_request_once_it_has_landed(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/landed")
    calls = fake_forge(monkeypatch, pr={**OPEN_PR, "state": "MERGED"})
    out = io.StringIO()
    naps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(interval=300, out=out, sleep=fake_sleep, repair=lambda conn, now: [])
    assert [c[1] for c in calls] == ["list"]  # three ticks, one query, then silence
    assert "prs:" not in out.getvalue()


def test_a_recorded_merge_is_never_looked_up_at_all(home, tmp_path, monkeypatch) -> None:
    conn = home
    task_id = delivered_task(conn, tmp_path, "harness/recorded")
    calls = fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pass"}])
    delivery.record_merged(task_id, "a" * 40)

    snap = watch.tick(conn)
    assert snap["prs"] == []  # the merge is on the record; the forge has nothing to add
    assert calls == []
    assert "prs:" not in watch.render(snap)


def test_a_tick_asks_the_forge_once_per_branch(home, tmp_path, monkeypatch) -> None:
    conn = home
    first = delivered_task(conn, tmp_path, "harness/shared")
    # A second task delivered onto the same branch must not cost a second lookup.
    second = delivered_task(conn, tmp_path, "harness/shared", status="worker_done")
    calls = fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pass"}])

    snap = watch.tick(conn)
    assert [e["task_id"] for e in snap["prs"]] == [first, second]
    assert [c[1] for c in calls] == ["list", "checks"]


def test_the_line_names_what_flipped_since_the_last_tick(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/flip")
    fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pending"}])

    first = watch.tick(conn)
    assert first["pr_changes"] == []  # nothing to compare the first tick against
    assert "changed:" not in watch.render(first)

    fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pass"}])
    second = watch.tick(conn, previous_prs=watch.pr_index(first["prs"]))
    assert second["pr_changes"] == ["PR #779 ci pending -> pass"]
    assert "changed: PR #779 ci pending -> pass" in watch.render(second)

    third = watch.tick(conn, previous_prs=watch.pr_index(second["prs"]))
    assert third["pr_changes"] == []  # steady state stays quiet


def test_run_compares_each_tick_with_the_one_before_it(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/loop")
    rounds = iter(
        [
            [{"name": "Unit tests", "bucket": "pending"}],
            [{"name": "Unit tests", "bucket": "pass"}],
        ]
    )

    def run_gh(args, cwd=None):
        if args[1] == "list":
            return (0, json.dumps([OPEN_PR]))
        return (0, json.dumps(next(rounds)))

    monkeypatch.setattr(watch, "_run_gh", run_gh)

    out = io.StringIO()
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(interval=300, out=out, sleep=fake_sleep, repair=lambda conn, now: [])
    lines = out.getvalue().strip().splitlines()
    assert "ci pending" in lines[0] and "changed:" not in lines[0]
    assert "changed: PR #779 ci pending -> pass" in lines[1]


IDLE = {"in_flight": [], "needs_me": [], "new_events": {}, "pr_changes": [], "prs": []}


def test_idle_means_every_reason_to_tick_is_absent() -> None:
    assert watch.is_idle(IDLE) is True
    assert watch.is_idle({**IDLE, "in_flight": [{"task_id": 1}]}) is False
    assert watch.is_idle({**IDLE, "needs_me": [{"id": 1, "status": "worker_done"}]}) is False
    assert watch.is_idle({**IDLE, "new_events": {"error": 1}}) is False
    assert watch.is_idle({**IDLE, "pr_changes": ["PR #1 ci pending -> fail"]}) is False
    assert watch.is_idle({**IDLE, "prs": [{"ci": "pending"}]}) is False
    assert watch.is_idle({**IDLE, "prs": [{"ci": "fail"}]}) is False
    # Green work, work the forge can't be asked about, and a full ledger are all
    # things the manager acts on in its own time — none of them is the team.
    assert watch.is_idle({**IDLE, "prs": [{"ci": "pass"}]}) is True
    assert watch.is_idle({**IDLE, "prs": [{"ci": "unknown"}]}) is True
    assert watch.is_idle({**IDLE, "open_todos": 4}) is True


def clock_from(start, step_minutes: int = 1):
    """A fake clock that advances one interval per tick."""
    ticks = count()

    def now():
        return start + timedelta(minutes=step_minutes * next(ticks))

    return now


def test_the_watch_says_it_is_going_quiet_once_then_says_nothing(home) -> None:
    conn = home
    out = io.StringIO()
    naps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 3:  # after the third idle tick, work arrives
            store.add_task(conn, run_id=store.create_run(conn, "new work"), title="build it")
        if len(naps) == 5:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(
            interval=300,
            out=out,
            sleep=fake_sleep,
            clock=clock_from(datetime(2026, 9, 4, 9, 0, tzinfo=UTC)),
        )

    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 4  # five ticks, one of them silent
    assert lines[0].startswith("TEAM 09:00 UTC — no workers in flight")
    assert lines[1] == "TEAM 09:01 UTC — idle; watch quiet until something changes"
    # The third idle tick (09:02) printed nothing at all; the dispatch brings the
    # normal line and the normal cadence straight back.
    assert lines[2].startswith("TEAM 09:03 UTC — in flight 1:")
    assert lines[3].startswith("TEAM 09:04 UTC — in flight 1:")
    assert naps == [300, 300, 300, 300, 300]  # the process never stopped ticking


def test_a_pull_request_still_running_its_checks_keeps_the_watch_talking(
    home, tmp_path, monkeypatch
) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/waiting")
    fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pending"}])
    out = io.StringIO()
    naps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(interval=300, out=out, sleep=fake_sleep)
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 3
    assert all("ci pending" in line for line in lines)


def test_exit_when_idle_ends_the_process_for_scripts(home) -> None:
    out = io.StringIO()
    naps: list[float] = []

    rc = watch.run(
        interval=300,
        exit_when_idle=True,
        out=out,
        sleep=naps.append,
        clock=clock_from(datetime(2026, 9, 4, 9, 0, tzinfo=UTC)),
    )
    assert rc == 0
    lines = out.getvalue().strip().splitlines()
    assert lines[0].startswith("TEAM 09:00 UTC — no workers in flight")
    assert lines[1] == "TEAM 09:01 UTC — idle; watch exiting (--exit-when-idle)"
    assert naps == [300]  # it slept once, then stopped rather than going quiet


def test_one_tick_is_always_spoken_even_when_idle(home) -> None:
    out = io.StringIO()
    assert watch.run(interval=1, once=True, out=out, sleep=lambda s: None) == 0
    assert out.getvalue().count("\n") == 1
    assert "idle; watch quiet" not in out.getvalue()


def test_cli_watch_exits_when_idle(home, capsys) -> None:
    assert cli.main(["watch", "--interval", "0", "--exit-when-idle"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert lines[1].endswith("idle; watch exiting (--exit-when-idle)")
