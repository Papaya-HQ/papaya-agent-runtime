"""The environment's command rules are stated by the runtime, not copied into briefs.

Claude workers run under a tool allowlist that matches one plain command per
call: pipes, `&&`, `;`, inline env assignments and redirection are denied, `cd`
must stand alone, and there is no `gh` — so a Claude worker cannot open a pull
request. Every brief was restating that by hand. Hand-copied rules drift.
"""

from __future__ import annotations

import time

import pytest

from papaya_agent_runtime import repos
from papaya_agent_runtime.config import MMConfig, WorkerCeiling, save_config
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.providers.claude import ClaudeAdapter
from papaya_agent_runtime.providers.codex import CodexAdapter
from papaya_agent_runtime.providers.command_rules import HEADING, command_rules
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer


def _spec(provider="claude", **kwargs):
    return TaskSpec(
        task_id=7,
        title="add the endpoint",
        instructions="THE BRIEF BODY",
        worktree_path="/tmp/wt",
        base_sha="abc1234",
        provider=provider,
        **kwargs,
    )


def _prompt(argv: list[str]) -> str:
    """The prompt argument both real adapters place third (`claude -p X`, `codex exec X`)."""
    return argv[2]


# --------------------------------------------------------------------------- #
# One source of truth
# --------------------------------------------------------------------------- #


def test_the_block_states_every_rule():
    text = command_rules("claude", "ppy/task-7-abc")
    assert HEADING in text
    for rule in ("|", "&&", ";", "FOO=1 cmd", ">", "cd", "Flagged, not done"):
        assert rule in text
    assert "git push origin HEAD:ppy/task-7-abc" in text
    assert "Do not try to open a pull request" in text
    assert "the manager opens the PR from it" in text


def test_the_block_tells_the_worker_to_run_the_suite_in_the_foreground():
    """A backgrounded suite dies with the session — task 103 lost a whole turn to it."""
    text = command_rules("claude", "ppy/task-7-abc")
    assert "in the foreground" in text
    assert "20 minutes" in text
    assert "never as a background task" in text


def test_the_block_falls_back_to_a_readable_branch_placeholder():
    assert "HEAD:<your task branch>" in command_rules("claude")


def test_no_other_provider_gets_the_block():
    assert command_rules("codex") == ""
    assert command_rules("codex", "ppy/task-7") == ""
    assert command_rules("fake") == ""


# --------------------------------------------------------------------------- #
# The dispatched prompt
# --------------------------------------------------------------------------- #


def test_a_claude_worker_prompt_leads_with_the_rules_then_the_brief():
    prompt = ClaudeAdapter().worker_prompt(_spec(branch="ppy/task-7-abc"))
    assert prompt.startswith("## " + HEADING)
    assert "git push origin HEAD:ppy/task-7-abc" in prompt
    assert prompt.index(HEADING) < prompt.index("THE BRIEF BODY")


def test_the_rules_survive_the_memory_preamble():
    spec = _spec(branch="ppy/task-7-abc")
    spec.memory_preamble = "REPO MEMORY"
    prompt = ClaudeAdapter().worker_prompt(spec)
    assert prompt.index(HEADING) < prompt.index("REPO MEMORY") < prompt.index("THE BRIEF BODY")


def test_the_dispatched_claude_argv_carries_the_block():
    argv = ClaudeAdapter().start(_spec(branch="ppy/task-7-abc"))
    assert HEADING in _prompt(argv)


def test_the_dispatched_codex_argv_does_not():
    argv = CodexAdapter().start(_spec(provider="codex", branch="ppy/task-7-abc"))
    prompt = _prompt(argv)
    assert HEADING not in prompt
    assert "Do not try to open a pull request" not in prompt
    assert prompt == "THE BRIEF BODY"


def test_a_resumed_claude_turn_does_not_repeat_the_block():
    """The rules were established when the session started; a steer is not a re-brief."""
    spec = _spec(branch="ppy/task-7-abc")
    spec.resume_session_id = "sess-1"
    spec.steer_message = "also update the changelog"
    assert HEADING not in " ".join(ClaudeAdapter().resume(spec))


# --------------------------------------------------------------------------- #
# End to end: the supervisor hands the adapter the branch to name
# --------------------------------------------------------------------------- #


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


def _capture_dispatch(server, source_repo, monkeypatch, provider):
    srv, _client = server
    model = "sonnet" if provider == "claude" else "gpt-5-codex"
    save_config(MMConfig(worker=WorkerCeiling(provider, model, "medium")))
    added = repos.add_repo(source_repo)
    captured: list[TaskSpec] = []
    monkeypatch.setattr(
        srv.supervisor, "_run_task", lambda runner, spec, **kw: captured.append(spec)
    )
    srv.supervisor.dispatch_task(
        repo=added.name, title="do it", instructions="THE BRIEF BODY", provider=provider
    )
    assert captured
    return captured[0]


def test_dispatch_gives_a_claude_worker_the_rules_naming_its_real_branch(
    server, source_repo, monkeypatch
):
    spec = _capture_dispatch(server, source_repo, monkeypatch, "claude")
    assert spec.branch and spec.branch.startswith("ppy/task-")
    prompt = ClaudeAdapter().worker_prompt(spec)
    assert HEADING in prompt
    assert f"git push origin HEAD:{spec.branch}" in prompt


def test_dispatch_leaves_a_codex_worker_prompt_alone(server, source_repo, monkeypatch):
    spec = _capture_dispatch(server, source_repo, monkeypatch, "codex")
    assert HEADING not in CodexAdapter().worker_prompt(spec)
