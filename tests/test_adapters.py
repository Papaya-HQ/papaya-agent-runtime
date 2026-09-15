"""Hermetic tests for Claude/Codex adapters using real M0 stream shapes.

No model is spawned; these feed captured event lines through the parsers.
"""

from __future__ import annotations

from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.providers.claude import ClaudeAdapter
from papaya_agent_runtime.providers.codex import CodexAdapter


def _spec(provider, **kw):
    return TaskSpec(
        task_id=1,
        title="t",
        instructions="do the thing",
        worktree_path="/tmp/wt",
        base_sha="abc",
        provider=provider,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #


def test_claude_start_argv_no_bypass() -> None:
    argv = ClaudeAdapter().start(_spec("claude", model="haiku"))
    assert argv[:2] == ["claude", "-p"]
    # The prompt leads with the environment's command rules (see
    # tests/test_command_rules.py) and ends with the brief itself.
    assert argv[2].endswith("do the thing")
    assert "stream-json" in argv
    assert "--model" in argv and "haiku" in argv
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_claude_start_prepends_memory_preamble() -> None:
    spec = _spec("claude", memory_preamble="Shared repo memory: read notes.md, log tasks.md")
    prompt = ClaudeAdapter().start(spec)[2]
    assert "Shared repo memory:" in prompt
    assert prompt.index("Shared repo memory:") < prompt.index("do the thing")


def test_claude_resume_uses_session() -> None:
    argv = ClaudeAdapter().resume(_spec("claude", resume_session_id="sid-9", model="haiku"))
    assert "--resume" in argv and "sid-9" in argv


def test_claude_result_and_usage() -> None:
    ad = ClaudeAdapter()
    lines = [
        '{"type":"system","subtype":"init","session_id":"s1"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done it",'
        '"session_id":"s1","usage":{"input_tokens":120,"output_tokens":30}}',
    ]
    events = [e for e in (ad.parse_event(x) for x in lines) if e]
    res = ad.result(events, 0)
    assert res.status == "completed"
    assert res.session_id == "s1"
    assert res.usage.input_tokens == 120 and res.usage.output_tokens == 30


def test_claude_error_result_is_failed() -> None:
    ad = ClaudeAdapter()
    lines = [
        '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"boom","session_id":"s1"}'
    ]
    events = [e for e in (ad.parse_event(x) for x in lines) if e]
    assert ad.result(events, 0).status == "failed"


def test_claude_missing_result_is_failed() -> None:
    ad = ClaudeAdapter()
    events = [e for e in (ad.parse_event('{"type":"system","session_id":"s1"}'),) if e]
    assert ad.result(events, 0).status == "failed"


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #


def test_codex_start_argv() -> None:
    argv = CodexAdapter().start(_spec("codex", model="gpt-5-codex", reasoning="medium"))
    assert argv[:3] == ["codex", "exec", "do the thing"]
    assert "--json" in argv
    assert "-s" in argv and "workspace-write" in argv
    assert "--model" in argv and "gpt-5-codex" in argv
    assert any("model_reasoning_effort" in a for a in argv)


def test_codex_resume_keeps_explicit_model_and_reasoning() -> None:
    argv = CodexAdapter().resume(
        _spec(
            "codex",
            resume_session_id="thread-9",
            model="gpt-5.6-sol",
            reasoning="high",
        )
    )
    assert argv[:3] == ["codex", "exec", "resume"]
    assert "--model" in argv and "gpt-5.6-sol" in argv
    assert 'model_reasoning_effort="high"' in argv


def test_codex_start_prepends_memory_preamble() -> None:
    spec = _spec("codex", memory_preamble="Shared repo memory: read notes.md, log tasks.md")
    prompt = CodexAdapter().start(spec)[2]
    assert prompt.startswith("Shared repo memory:")
    assert "do the thing" in prompt


def test_codex_result_and_usage() -> None:
    ad = CodexAdapter()
    lines = [
        '{"type":"thread.started","thread_id":"th-1"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"all set"}}',
        '{"type":"turn.completed","usage":{"input_tokens":2000,"output_tokens":15}}',
    ]
    events = [e for e in (ad.parse_event(x) for x in lines) if e]
    res = ad.result(events, 0)
    assert res.status == "completed"
    assert res.session_id == "th-1"
    assert res.summary == "all set"
    assert res.usage.input_tokens == 2000


def test_codex_no_completion_is_failed() -> None:
    ad = CodexAdapter()
    lines = ['{"type":"thread.started","thread_id":"th-1"}', '{"type":"turn.started"}']
    events = [e for e in (ad.parse_event(x) for x in lines) if e]
    assert ad.result(events, 1).status == "failed"
