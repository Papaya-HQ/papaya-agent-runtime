"""Hermetic tests for report rendering and provider event parsing."""

from __future__ import annotations

from papaya_agent_runtime.probes.capability import CapabilityRecord, ScenarioResult
from papaya_agent_runtime.probes.providers import _claude_session_id, _codex_session_id
from papaya_agent_runtime.probes.report import render_markdown


def test_render_markdown_empty() -> None:
    out = render_markdown([])
    assert "Provider capability matrix" in out
    assert "No providers" in out


def test_render_markdown_includes_flags_and_scenarios() -> None:
    record = CapabilityRecord(
        provider="codex",
        cli_version="0.150.1",
        probed_at="2026-08-28T00:00:00+00:00",
        probe_tool_version="0.0.0",
        model=None,
        scenarios=[ScenarioResult("clean_complete", "proved", "ok")],
    )
    record.capabilities.session_id_in_stream = True
    out = render_markdown([record])
    assert "codex 0.150.1" in out
    assert "`session_id_in_stream`" in out
    assert "clean_complete" in out
    # Fail-closed flag renders as no.
    assert "| yes |" in out or "| yes " in out


def test_claude_session_id_from_stream() -> None:
    lines = [
        '{"type":"system","subtype":"init","session_id":"abc-123"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}',
    ]
    assert _claude_session_id(lines) == "abc-123"


def test_codex_thread_id_from_stream() -> None:
    lines = [
        '{"type":"thread.started","thread_id":"019dd4bf-0929"}',
        '{"type":"turn.started"}',
    ]
    assert _codex_session_id(lines) == "019dd4bf-0929"


def test_session_id_ignores_non_json_noise() -> None:
    lines = ["warning: something", '{"session_id":"zzz"}']
    assert _claude_session_id(lines) == "zzz"
