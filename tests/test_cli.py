"""Hermetic tests for the ppy CLI dispatch."""

from __future__ import annotations

import json
import subprocess

import pytest

from papaya_agent_runtime import cli, repos
from papaya_agent_runtime.config import MMConfig, WorkerCeiling, load_config, save_config


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


@pytest.fixture
def sent_dispatch(monkeypatch):
    """Capture what `ppy dispatch` hands the supervisor, without a supervisor."""
    sent: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            sent.update(kwargs)
            return {"ok": True, "task_id": 7, "run_id": 3, "branch": "ppy/task-7-abc"}

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    return sent


def test_version(capsys) -> None:
    assert cli.main(["version"]) == 0
    assert "papaya-agent-runtime" in capsys.readouterr().out


def test_status_without_setup(ppy_home, capsys) -> None:
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "config:" in out


def test_repo_add_and_list_via_cli(tmp_path, ppy_home, capsys) -> None:
    source = tmp_path / "src"
    source.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "t"],
        ["config", "user.email", "t@t"],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    (source / "f").write_text("x")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "i"], check=True)

    assert cli.main(["repo", "add", str(source)]) == 0
    capsys.readouterr()
    assert cli.main(["repo", "list"]) == 0
    assert "src" in capsys.readouterr().out


def test_assessment_policy_is_easy_to_reconfigure(ppy_home, capsys) -> None:
    save_config(MMConfig())
    assert (
        cli.main(
            [
                "config",
                "assessments",
                "--completed-runs",
                "8",
                "--max-days",
                "21",
                "--disabled",
            ]
        )
        == 0
    )
    capsys.readouterr()
    policy = load_config().assessments
    assert policy.enabled is False
    assert policy.completed_runs == 8
    assert policy.max_days == 21


def test_assessment_cli_round_trip(ppy_home, capsys) -> None:
    assert cli.main(["assessment", "tick", "--force", "--json"]) == 0
    cycle_id = json.loads(capsys.readouterr().out)["id"]
    actions = json.dumps(
        [
            {
                "description": "Review worker plans earlier.",
                "observation": "Late feedback caused rework.",
                "likely_cause": "The manager waited for completion.",
                "baseline": "No early checks.",
                "target": "Check every multi-step task once.",
                "measurement": "Early checks per multi-step task.",
            }
        ]
    )
    assert (
        cli.main(
            [
                "assessment",
                "complete",
                str(cycle_id),
                "--summary",
                "Delivery is sound; feedback should land earlier.",
                "--strength",
                "Kept the work moving.",
                "--weakness",
                "Caught one issue late.",
                "--action-json",
                actions,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "assessment",
                "align",
                str(cycle_id),
                "--decision",
                "approved",
                "--notes",
                "Run it for the next cycle.",
            ]
        )
        == 0
    )
    assert "aligned" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Which provider an unnamed dispatch gets (issue #49)
# --------------------------------------------------------------------------- #


#: The distinctive part of the fake-on-a-remote warning. Matching this rather than
#: the bare word "warning" keeps the assertion off the tmp path in the same output.
FAKE_REMOTE_WARNING = "the fake worker writes a stub"


def _claude_config() -> None:
    save_config(MMConfig(worker=WorkerCeiling("claude", "opus", "medium")))


@pytest.mark.real_provider_default
def test_dispatch_without_a_provider_uses_the_configured_worker_provider(
    ppy_home, sent_dispatch, capsys
) -> None:
    _claude_config()

    assert cli.main(["dispatch", "--repo", "papaya", "--title", "t", "--instructions", "go"]) == 0

    capsys.readouterr()
    assert sent_dispatch["provider"] == "claude"


@pytest.mark.real_provider_default
def test_dispatch_still_takes_fake_when_it_is_asked_for(ppy_home, sent_dispatch, capsys) -> None:
    _claude_config()

    assert (
        cli.main(
            [
                "dispatch",
                "--repo",
                "papaya",
                "--title",
                "t",
                "--instructions",
                "go",
                "--provider",
                "fake",
            ]
        )
        == 0
    )

    capsys.readouterr()
    assert sent_dispatch["provider"] == "fake"


@pytest.mark.real_provider_default
def test_the_supervisor_records_the_configured_provider_when_none_is_named(
    ppy_home, source_repo, monkeypatch
) -> None:
    """The provider reaches the task row, not just the wire."""
    from papaya_agent_runtime.providers.fake import FakeProvider
    from papaya_agent_runtime.state import init_db, store
    from papaya_agent_runtime.supervisor import core as core_mod

    _claude_config()
    added = repos.add_repo(source_repo)
    # The recorded provider is the subject; no real CLI is spawned to prove it.
    monkeypatch.setattr(core_mod, "_adapter_for", lambda provider: FakeProvider())

    resp = core_mod.Supervisor().dispatch_task(
        repo=added.name, title="unnamed provider", instructions="NOPUSH NODONE"
    )

    task = store.get_task(init_db(), resp["task_id"])
    assert task["provider"] == "claude"


@pytest.mark.real_provider_default
def test_dispatch_warns_when_fake_would_run_against_a_repo_with_a_remote_origin(
    ppy_home, source_repo, sent_dispatch, capsys
) -> None:
    _claude_config()
    added = repos.add_repo(source_repo)
    remote = "https://example.invalid/papaya.git"
    subprocess.run(
        ["git", "-C", added.local_path, "remote", "set-url", "origin", remote], check=True
    )

    rc = cli.main(
        [
            "dispatch",
            "--repo",
            added.name,
            "--title",
            "t",
            "--instructions",
            "go",
            "--provider",
            "fake",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert FAKE_REMOTE_WARNING in out
    assert remote in out


@pytest.mark.real_provider_default
def test_no_warning_for_fake_against_a_local_origin(
    ppy_home, source_repo, sent_dispatch, capsys
) -> None:
    _claude_config()
    added = repos.add_repo(source_repo)

    rc = cli.main(
        [
            "dispatch",
            "--repo",
            added.name,
            "--title",
            "t",
            "--instructions",
            "go",
            "--provider",
            "fake",
        ]
    )

    assert rc == 0
    assert FAKE_REMOTE_WARNING not in capsys.readouterr().out


def test_papaya_connect_refuses_create_engineer_beside_a_named_agent() -> None:
    """The client exits 2 on the pair; the parser says so before anything is installed."""
    from papaya_agent_runtime import cli

    parser = cli.build_parser()
    assert parser.parse_args(["papaya", "connect", "--create-engineer"]).create_engineer is True
    assert parser.parse_args(["setup", "--create-engineer"]).create_engineer is True
    with pytest.raises(SystemExit):
        parser.parse_args(["papaya", "connect", "--create-engineer", "--agent", "Bea"])
