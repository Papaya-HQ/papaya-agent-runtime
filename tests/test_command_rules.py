"""The environment's command rules are stated by the runtime, not copied into briefs.

Claude workers run under a tool allowlist that matches one plain command per
call: pipes, `&&`, `;`, inline env assignments and redirection are denied, `cd`
must stand alone, and there is no `gh` — so a Claude worker cannot open a pull
request. Every brief was restating that by hand. Hand-copied rules drift.
"""

from __future__ import annotations

import time

import pytest

from papaya_agent_runtime import prompts, repos, tool_learning
from papaya_agent_runtime.config import MMConfig, WorkerCeiling, save_config
from papaya_agent_runtime.providers import command_rules as command_rules_module
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


#: The commands issue #127 records, verbatim from the runtime's own ledger. Every one
#: is a worker copying ITS OWN session's saved output into its evidence directory.
REFUSED_COPIES = (
    (
        "cp /Users/x/.claude/projects/-Users-x--treehouse-fe-bbcd55-2-fe/"
        "e016105b-c59d-4df4-9ca4-8772fea6f1eb/tool-results/baukveark.txt "
        "/Users/x/.treehouse/fe-bbcd55/2/fe/.ppy-evidence/blocks-build.txt",
        "blocks-build.txt",
    ),
    (
        'cp "/Users/x/.claude/projects/-Users-x--treehouse-fe-bbcd55-2-fe/'
        'dfbdaa86-4b18-4b4f-ad65-77345a141f6c/tool-results/b6s2oukvq.txt" '
        '"/Users/x/.treehouse/fe-bbcd55/2/fe/.ppy-evidence/ios-build-1890a0aa.txt"',
        "ios-build-1890a0aa.txt",
    ),
    (
        'cp "/Users/x/.claude/projects/-Users-x--treehouse-pa-19e1b0-1-pa/'
        '40e89601-3930-4195-8832-943dd6330015/tool-results/bsit98i9i.txt" '
        "/tmp/mutation-revert-output.txt\nwc -l /tmp/mutation-revert-output.txt",
        "mutation-revert-output.txt",
    ),
)


def test_the_rules_tell_a_worker_how_to_keep_a_long_commands_output():
    """Issue #127: the refusals were right and left no sanctioned way to keep a receipt."""
    text = command_rules("claude", "ppy/task-7-abc")

    assert "ppy evidence add" in text and "tool-results" in text
    assert "Do NOT `cp` it" in text


@pytest.mark.parametrize(("refused", "name"), REFUSED_COPIES)
def test_each_refused_copy_from_issue_127_gets_the_exact_command_to_run_instead(refused, name):
    rewrite = command_rules_module.rewrite_for(refused, "/Users/x/.treehouse/fe-bbcd55/2/fe", 180)

    assert rewrite is not None and rewrite.runnable
    assert rewrite.instead.startswith("`ppy evidence add /Users/x/.claude/projects/")
    assert f"--task 180 --as {name}`" in rewrite.instead
    # Never "split it into two calls": the third of these carries a newline, and the
    # answer to both halves is the one command, not the shape rule.
    assert "one command per call" not in rewrite.instead


def test_a_refused_copy_is_not_a_profile_gap_because_cp_must_stay_refused():
    refused, _name = REFUSED_COPIES[0]

    verdict = tool_learning.classify("Bash", refused, "/Users/x/.treehouse/fe-bbcd55/2/fe")

    assert verdict.kind == tool_learning.COMMAND_SHAPE
    assert not verdict.in_family
    assert "ppy evidence add" in verdict.reason


def test_the_evidence_command_is_never_rewritten_into_itself():
    already = (
        "ppy evidence add /Users/x/.claude/projects/-p/"
        "e016105b-c59d-4df4-9ca4-8772fea6f1eb/tool-results/a.txt --task 5 --as a.txt"
    )

    assert command_rules_module.rewrite_for(already, "/Users/x/wt", 5) is None


def test_the_block_tells_the_worker_to_run_the_suite_in_the_foreground():
    """A backgrounded suite dies with the session — task 103 lost a whole turn to it."""
    text = command_rules("claude", "ppy/task-7-abc")
    assert "in the foreground" in text
    assert "never as a background task" in text
    # A 20-minute tool timeout was never achievable: the harness caps a call at ten
    # minutes and backgrounds the rest (PAP-213), so the rule names `ppy gate run`.
    assert "20 minutes" not in text
    assert " ".join(prompts.TEN_MINUTE_RULE.split()) in " ".join(text.split())


def test_the_brief_the_environment_block_and_the_rules_carry_the_push_milestone_rule():
    """PAP-219: two hours of finished work held for one commit, on one machine's disk."""
    from papaya_agent_runtime import environment

    def flat(text: str) -> str:
        return " ".join(text.split())

    rule = flat(prompts.PUSH_MILESTONE_RULE)
    block = environment.render(
        environment.RepoEnvironment(repo="app"),
        task_id=7,
        evidence_path="/tmp/wt/.ppy-evidence",
        branch="ppy/task-7-abc",
    )
    texts = {
        "brief.md": prompts.load(prompts.BRIEF),
        "environment block": block,
        "command rules": command_rules("claude", "ppy/task-7-abc"),
    }
    for where, text in texts.items():
        assert rule in flat(text), where
        # Beside the ten-minute rule, which it names as the latest moment to push.
        assert flat(prompts.TEN_MINUTE_RULE) in flat(text), where


def test_every_worker_facing_text_names_the_three_gate_tiers():
    """2026-09-17: the full suite ran at every milestone, every hand-back and the review."""
    from papaya_agent_runtime import environment

    def flat(text: str) -> str:
        return " ".join(text.split())

    block = environment.render(
        environment.RepoEnvironment(repo="app"), task_id=7, evidence_path="/tmp/wt/.ppy-evidence"
    )
    texts = {
        "brief.md": prompts.load(prompts.BRIEF),
        "review.md": prompts.load(prompts.REVIEW),
        "checkin.md": prompts.load(prompts.CHECKIN),
        "environment block": block,
        "command rules": command_rules("claude", "ppy/task-7-abc"),
    }
    for where, text in texts.items():
        assert flat(prompts.GATE_TIERS_RULE) in flat(text), where
    # The milestone push waits on the scoped gate, and says so.
    assert "(the scoped gate, never the full suite)" in prompts.PUSH_MILESTONE_RULE
    assert "make verify" not in prompts.PUSH_MILESTONE_RULE
    # The brief turn quotes the repository's own words and never guesses a gate.
    brief = flat(prompts.load(prompts.BRIEF))
    assert "quoting the repository's own words and naming the file each came from" in brief
    assert "do not guess one" in brief
    # The review turn never reruns a full suite already recorded at the head.
    review = flat(prompts.load(prompts.REVIEW))
    assert "Do not run `ppy gate run --full` again at a head that has one" in review


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
