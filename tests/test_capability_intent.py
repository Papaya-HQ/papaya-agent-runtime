"""The safe family is per-install config, an approval joins it, and grants follow intent."""

from __future__ import annotations

import pytest

from papaya_agent_runtime import capability_requests as cr
from papaya_agent_runtime import config_changes, tool_learning
from papaya_agent_runtime.config import ConfigError, MMConfig, load_config, save_config
from papaya_agent_runtime.providers.claude import ALLOWED_TOOLS_ENV
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def home(ppy_home, monkeypatch):
    monkeypatch.delenv(ALLOWED_TOOLS_ENV, raising=False)
    save_config(MMConfig())
    monkeypatch.setattr(tool_learning, "steer_worker", lambda t, m: None)


def _task() -> int:
    conn = init_db()
    try:
        task_id = store.add_task(conn, run_id=store.create_run(conn, "ios"), title="t")
        store.set_task_status(conn, task_id, "in_progress")
        return task_id
    finally:
        conn.close()


def _configure(**fields) -> None:
    cfg = load_config()
    for name, value in fields.items():
        setattr(cfg.capabilities, name, value)
    save_config(cfg)


# ── the family lives in config ──────────────────────────────────────────────


def test_the_default_family_is_unchanged_without_config(home) -> None:
    assert tool_learning.safe_family() == tool_learning.DEFAULT_SAFE_FAMILY


def test_an_install_adds_and_drops_programs_and_it_survives_a_save(home) -> None:
    _configure(safe_family={"xcodegen2": "run", "zig": "run"}, drop_family=["make"])

    family = tool_learning.safe_family()
    assert family["zig"] == "run" and "make" not in family
    assert load_config().capabilities.safe_family == {"xcodegen2": "run", "zig": "run"}
    verdict = tool_learning.classify("Bash", "zig build", "/w")
    assert verdict.in_family and verdict.pattern == "Bash(zig:*)"
    assert not tool_learning.classify("Bash", "make test", "/w").in_family


def test_the_family_stays_closed_whatever_the_config_says(home) -> None:
    cfg = load_config()
    cfg.capabilities.safe_family = {"sudo": "run"}
    with pytest.raises(ConfigError):
        cfg.validate()
    # A file edited by hand around validation still cannot make it learnable.
    cfg.capabilities.safe_family = {}
    assert "sudo" not in tool_learning.safe_family(_Hand({"sudo": "run", "curl": "read"}))
    assert not tool_learning.classify("Bash", "curl example.com", "/w").in_family


class _Hand:
    drop_family: list[str] = []

    def __init__(self, family: dict[str, str]) -> None:
        self.safe_family = family


@pytest.mark.parametrize("bad", [{"zig": "conditional"}, {"zig": "sudo"}, {"a b": "run"}])
def test_a_family_entry_names_a_program_and_a_kind(home, bad) -> None:
    cfg = load_config()
    cfg.capabilities.safe_family = bad
    with pytest.raises(ConfigError):
        cfg.validate()


def test_a_configured_write_program_is_still_held_to_the_worktree(home) -> None:
    _configure(safe_family={"ditto": "write"})

    outside = tool_learning.classify("Bash", "ditto a /elsewhere/b", "/w")

    assert not outside.in_family and outside.kind == tool_learning.OUTSIDE_WORKTREE
    assert tool_learning.classify("Bash", "ditto a b", "/w").in_family


# ── an approval joins the family ────────────────────────────────────────────


def test_approving_a_program_learns_it_for_the_next_worker(home) -> None:
    first = cr.request(_task(), "terraform")
    assert first.state == cr.PENDING

    cr.decide_request(first.id, approve=True)

    assert load_config().capabilities.safe_family == {"terraform": "run"}
    assert cr.request(_task(), "terraform").state == cr.AUTO_GRANTED
    assert tool_learning.classify("Bash", "terraform plan", "/w").in_family
    assert any(c["key"] == "capabilities.safe_family" for c in config_changes.history())


def test_a_denial_learns_nothing(home) -> None:
    request = cr.request(_task(), "terraform")

    cr.decide_request(request.id, approve=False, reason="not this machine")

    assert load_config().capabilities.safe_family == {}


def test_an_approval_is_not_learned_when_the_install_opts_out_or_dropped_it(home) -> None:
    _configure(learn_approvals=False)
    request = cr.request(_task(), "terraform")
    cr.decide_request(request.id, approve=True)
    assert load_config().capabilities.safe_family == {}

    _configure(learn_approvals=True, drop_family=["packer"])
    request = cr.request(_task(), "packer")
    cr.decide_request(request.id, approve=True)
    assert load_config().capabilities.safe_family == {}


def test_a_path_or_a_tool_is_never_learned_from_an_approval(home) -> None:
    by_path = cr.request(
        _task(), "python", path="/opt/x/bin/python", reach=cr.ABSOLUTE, resolved="/opt/x/bin/python"
    )
    tool = cr.request(_task(), "WebSearch")
    cr.decide_request(by_path.id, approve=True)
    cr.decide_request(tool.id, approve=True)

    assert load_config().capabilities.safe_family == {}


# ── intent, not only names ──────────────────────────────────────────────────


def test_a_versioned_variant_of_a_granted_program_is_granted_without_asking(home) -> None:
    task_id = _task()

    variant = cr.request(task_id, "python3.12")

    assert variant.state == cr.AUTO_GRANTED
    assert "variant of `python`" in (variant.reason or "")
    assert "Bash(python3.12:*)" in load_config().claude.extra_tools


def test_a_variant_of_an_approved_program_follows_the_approval(home) -> None:
    request = cr.request(_task(), "terraform")
    cr.decide_request(request.id, approve=True)

    assert cr.request(_task(), "terraform1.6").state == cr.AUTO_GRANTED
    assert tool_learning.classify("Bash", "terraform1.6 plan", "/w").in_family


def test_intent_grants_can_be_turned_off(home) -> None:
    _configure(intent_grants=False)

    assert cr.request(_task(), "python3.12").state == cr.PENDING
    assert not tool_learning.classify("Bash", "python3.12 x.py", "/w").in_family


def test_a_variant_of_a_refused_or_conditional_program_is_not_granted(home) -> None:
    _configure(never=["psql"])

    assert cr.request(_task(), "sudo2").state == cr.PENDING
    assert cr.request(_task(), "bash5").state == cr.PENDING
    assert cr.request(_task(), "psql16").state == cr.PENDING
    assert cr.request(_task(), "sed4").state == cr.PENDING
    assert cr.request(_task(), "unrelated").state == cr.PENDING


def test_a_variant_is_still_asked_about_when_named_by_path_outside_the_worktree(home) -> None:
    request = cr.request(
        _task(), "python3.12", path="/usr/bin/python3.12", reach=cr.ABSOLUTE, resolved="/usr/bin"
    )

    assert request.state == cr.PENDING
