"""Contract-heavy briefs go to the top tier by default; the dispatch says why (runtime #94.6)."""

from __future__ import annotations

import json
import shutil
from collections import namedtuple

import pytest

from conftest import wait_until
from papaya_agent_runtime import cli, repos, routing
from papaya_agent_runtime.config import MMConfig, WorkerCeiling, save_config
from papaya_agent_runtime.providers.fake import FakeProvider
from papaya_agent_runtime.state import init_db
from papaya_agent_runtime.supervisor import core
from papaya_agent_runtime.supervisor.core import Supervisor, SupervisorError


def _config() -> MMConfig:
    cfg = MMConfig(worker=WorkerCeiling("claude", "opus", "high", "sonnet", "medium", 2))
    save_config(cfg)
    return cfg


MIGRATION_PATH = "# Events\n\n## In scope\n\nEdit `backend/alembic/versions/0042_events.py`.\n"
DJANGO_PATH = "# Events\n\n## In scope\n\nWrite `app/migrations/0007_events.py`.\n"
DOWN_REVISION = "# Events\n\n## In scope\n\nSet `down_revision` to the current head.\n"
ADD_MIGRATION = "# Events\n\n## In scope\n\nAdd a migration for the events table.\n"
NEW_MIGRATION = "# Events\n\n## In scope\n\nThe new Alembic migration creates the table.\n"
ROUTES_BRIEF = "# Feed\n\n## In scope\n\nExpose two new routes under /api/feed.\n"
STATE_BRIEF = "# Retry\n\n## Semantics\n\nThe back-off is a state machine; table below.\n"
FOLLOW_UP = "# Rename a helper\n\n## In scope\n\nRename `foo` to `bar` in one file.\n"
ADVISORY_ONLY = (
    "# Quieter dispatch output\n\n## In scope\n\n"
    "Print the migration advisory after the overlap advisory, and word the "
    "migration advisory so it names the other task's branch.\n"
)

MIGRATION = "adds a database migration"


@pytest.mark.parametrize(
    "brief", [MIGRATION_PATH, DJANGO_PATH, DOWN_REVISION, ADD_MIGRATION, NEW_MIGRATION]
)
def test_an_actual_migration_is_a_contract_signal(brief: str) -> None:
    assert routing.contract_signals(brief) == [MIGRATION]


def test_each_other_signal_names_its_reason_and_a_follow_up_has_none() -> None:
    assert routing.contract_signals(ROUTES_BRIEF) == ["names new routes or endpoints"]
    assert routing.contract_signals(STATE_BRIEF) == ["specifies a state machine"]
    assert routing.contract_signals(FOLLOW_UP) == []
    # "route" alone is not a signal; adding one is.
    assert routing.contract_signals("Fix the typo on the settings route.") == []


def test_mentioning_the_migration_advisory_is_not_a_migration() -> None:
    # Middle Manager's bare-word match sent this brief to the top tier.
    assert routing.contract_signals(ADVISORY_ONLY) == []
    for text in (
        "Keep the migration advisory as it is.",
        "The migration collision check reads `migrations.py`.",
        "Update `src/papaya_agent_runtime/migrations.py` to log the reason.",
        "Add the migration advisory to the dispatch output.",
        "A new migration advisory line for stacked tasks.",
        "Migrations are out of scope.",
    ):
        assert routing.contract_signals(text) == [], text


def test_more_than_five_numbered_items_under_in_scope_is_a_contract() -> None:
    items = "\n".join(f"{i}. item {i}" for i in range(1, 7))
    brief = f"# Big\n\n## In scope\n\n{items}\n\n## Out of scope\n\n1. not this\n2. nor this\n"
    assert routing.contract_signals(brief) == ["lists 6 numbered scope items"]
    five = "\n".join(f"{i}. item {i}" for i in range(1, 6))
    assert routing.contract_signals(f"# Small\n\n## In scope\n\n{five}\n") == []
    # Numbered items outside In scope do not count, Out of scope included.
    steps = "\n".join(f"{i}. step {i}" for i in range(1, 9))
    assert routing.contract_signals(f"# Steps\n\n## Plan\n\n{steps}\n") == []
    assert routing.contract_signals(f"# Steps\n\n## Out of scope\n\n{steps}\n") == []


def test_route_picks_the_ceiling_for_a_contract_and_the_default_otherwise(ppy_home) -> None:
    cfg = _config()
    top = routing.route(
        cfg, provider="claude", instructions=ADD_MIGRATION, model=None, reasoning=None
    )
    assert (top.model, top.reasoning, top.tier) == ("opus", "high", "top-tier")
    assert top.rule == f"contract-heavy brief: {MIGRATION}"
    assert routing.describe(top) == (
        f"routing: top-tier (opus/high) — contract-heavy brief: {MIGRATION}"
    )

    for brief in (FOLLOW_UP, ADVISORY_ONLY):
        small = routing.route(
            cfg, provider="claude", instructions=brief, model=None, reasoning=None
        )
        assert (small.model, small.reasoning, small.tier) == ("sonnet", "medium", "default")


def test_an_explicit_model_or_reasoning_wins(ppy_home) -> None:
    cfg = _config()
    pinned = routing.route(
        cfg, provider="claude", instructions=ADD_MIGRATION, model="haiku", reasoning=None
    )
    assert (pinned.model, pinned.reasoning, pinned.tier) == ("haiku", "medium", "explicit")
    low = routing.route(
        cfg, provider="claude", instructions=ADD_MIGRATION, model=None, reasoning="low"
    )
    assert (low.model, low.reasoning, low.tier) == ("sonnet", "low", "explicit")
    # A brief for another provider is never routed onto this ceiling's ladder.
    other = routing.route(
        cfg, provider="codex", instructions=ADD_MIGRATION, model=None, reasoning=None
    )
    assert other.tier == "default"


def _capture_runs(supervisor: Supervisor, monkeypatch) -> list:
    captured = []
    monkeypatch.setattr(core, "_adapter_for", lambda provider: FakeProvider())

    def capture(runner, spec, execution=None):
        captured.append(spec)
        supervisor._release(execution)

    monkeypatch.setattr(supervisor, "_run_task", capture)
    return captured


def _dispatched_payload(task_id: int) -> dict:
    row = (
        init_db()
        .execute("SELECT payload FROM events WHERE task_id = ? AND kind = 'dispatched'", (task_id,))
        .fetchone()
    )
    return json.loads(row["payload"])


def test_dispatch_routes_and_records_the_rule(ppy_home, source_repo, monkeypatch) -> None:
    _config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    captured = _capture_runs(supervisor, monkeypatch)

    heavy = supervisor.dispatch_task(
        repo=added.name, title="events", instructions=ROUTES_BRIEF, provider="claude"
    )
    line = "routing: top-tier (opus/high) — contract-heavy brief: names new routes or endpoints"
    assert heavy["routing"]["tier"] == "top-tier"
    assert heavy["routing"]["line"] == line
    wait_until(lambda: len(captured) == 1, 5.0, what="heavy worker", interval=0.01)
    assert (captured[0].model, captured[0].reasoning) == ("opus", "high")
    assert _dispatched_payload(heavy["task_id"])["routing"]["line"] == line

    light = supervisor.dispatch_task(
        repo=added.name, title="rename", instructions=ADVISORY_ONLY, provider="claude"
    )
    assert light["routing"]["tier"] == "default"
    assert light["routing"]["line"].startswith("routing: default (sonnet/medium) — ")
    wait_until(lambda: len(captured) == 2, 5.0, what="light worker", interval=0.01)
    assert (captured[1].model, captured[1].reasoning) == ("sonnet", "medium")
    assert _dispatched_payload(light["task_id"])["routing"]["tier"] == "default"

    pinned = supervisor.dispatch_task(
        repo=added.name,
        title="feed again",
        instructions=STATE_BRIEF,
        provider="claude",
        model="haiku",
        reasoning="low",
    )
    assert pinned["routing"]["tier"] == "explicit"
    wait_until(lambda: len(captured) == 3, 5.0, what="pinned worker", interval=0.01)
    assert (captured[2].model, captured[2].reasoning) == ("haiku", "low")


def test_nothing_routes_above_the_ceiling(ppy_home, source_repo) -> None:
    _config()
    added = repos.add_repo(source_repo)
    # The top tier is the ceiling; asking past it is still refused before any task row.
    with pytest.raises(SupervisorError, match="exceeds ceiling"):
        Supervisor().dispatch_task(
            repo=added.name,
            title="too much",
            instructions=ROUTES_BRIEF,
            provider="claude",
            reasoning="xhigh",
        )
    assert init_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    cfg = _config()
    top = routing.route(
        cfg, provider="claude", instructions=ROUTES_BRIEF, model=None, reasoning=None
    )
    assert (top.model, top.reasoning) == (cfg.worker.max_model, cfg.worker.max_reasoning)


Usage = namedtuple("Usage", "total used free")


def test_ppy_dispatch_prints_the_routing_line(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    (tmp_path / ".ppy").mkdir()
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 20e9, 80e9))
    line = "routing: top-tier (opus/high) — contract-heavy brief: specifies a state machine"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            return {
                "ok": True,
                "task_id": 7,
                "run_id": 3,
                "branch": "ppy/task-7-abc",
                "routing": {"tier": "top-tier", "line": line},
            }

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    rc = cli.main(["dispatch", "--repo", "papaya", "--title", "retry", "--instructions", "go"])
    assert rc == 0
    assert line in capsys.readouterr().out.splitlines()
