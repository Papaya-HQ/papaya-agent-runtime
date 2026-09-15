"""The plain-references gate: replies to the user describe things, never cite labels."""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import hooks, plain_language
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    monkeypatch.delenv("PPY_DEV", raising=False)
    return tmp_path / ".ppy"


def test_findings_catch_the_label_shapes_in_order() -> None:
    text = (
        "Plan §2 is wrong; §6.3 still says 100 ms. 1C may land before 1B; migration "
        "order now 1C, 1B, 1D, 2B2, 2B3. This informs 1B/1D. Built against rev3."
    )
    assert plain_language.findings(text) == ["§2", "§6.3", "1C", "1B", "1D", "2B2", "2B3", "rev3"]


def test_plain_prose_passes() -> None:
    text = (
        "The plan step that adds per-item summaries and links (the second backend PR) "
        "starts the day the continuity PR merges. Two hundred situations, p95 under 100 ms. "
        "PR #730 is green; issue 42 is closed. The 2024 budget and Q3 targets are unchanged."
    )
    assert plain_language.findings(text) == []


def test_labels_are_allowed_in_code_urls_and_parentheticals() -> None:
    text = (
        "The plan step that adds the projection table (the plan calls it 1B, §6) is next. "
        "See `docs/plan.md §5` and https://example.com/rev3/1C for the source.\n"
        "```\n1A §4 rev2\n```\n"
        "[the plan](https://example.com/plans/2B2) covers it."
    )
    assert plain_language.findings(text) == []


def test_block_reason_names_the_labels_and_the_fix() -> None:
    reason = plain_language.block_reason(["1B", "§5"])
    assert reason is not None
    assert "1B, §5" in reason
    assert "described as what it is" in reason
    assert "reply again" in reason
    assert plain_language.block_reason([]) is None


def test_last_assistant_text_prefers_payload_then_transcript(tmp_path) -> None:
    assert plain_language.last_assistant_text({"last_assistant_message": "hi §1"}) == "hi §1"

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "go"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "tool_use", "name": "Bash"},
                                {"type": "text", "text": "earlier reply about 1A"},
                            ]
                        },
                    }
                ),
                "not json",
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "final reply about 1B"}]},
                    }
                ),
            ]
        )
    )
    assert plain_language.last_assistant_text({"transcript_path": str(transcript)}) == (
        "final reply about 1B"
    )
    assert plain_language.last_assistant_text({"transcript_path": str(tmp_path / "nope")}) == ""
    assert plain_language.last_assistant_text({}) == ""


def test_stop_hook_bounces_a_labelled_reply_once(ppy_home) -> None:
    init_db()
    labelled = {"last_assistant_message": "Kicked off 1C; it depends on §5 of rev3."}

    first = hooks.handle_hook_stdin("Stop", json.dumps(labelled))
    assert first["decision"] == "block"
    assert "1C, §5, rev3" in first["reason"]

    # The harness re-fires with stop_hook_active once it has blocked; never loop.
    again = hooks.handle_hook_stdin("Stop", json.dumps({**labelled, "stop_hook_active": True}))
    assert "decision" not in again

    plain = {
        "last_assistant_message": (
            "Kicked off the worker-corrections PR; it builds on the continuity work "
            "(the plan calls that step 1C)."
        )
    }
    assert "decision" not in hooks.handle_hook_stdin("Stop", json.dumps(plain))


def test_stop_hook_combines_ledger_and_plain_reasons(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build")
    store.set_task_status(conn, task_id, "worker_done")

    result = hooks.handle_hook_stdin(
        "Stop", json.dumps({"last_assistant_message": "Done with 1B."})
    )
    assert result["decision"] == "block"
    assert "no next step is recorded" in result["reason"]
    assert "internal label: 1B" in result["reason"]


def test_stop_hook_skips_the_gate_in_dev_sessions(ppy_home, monkeypatch) -> None:
    init_db()
    monkeypatch.setenv("PPY_DEV", "1")
    result = hooks.handle_hook_stdin("Stop", json.dumps({"last_assistant_message": "1B §5"}))
    assert "decision" not in result
