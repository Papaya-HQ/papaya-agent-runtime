"""The outbound sanitiser: tool-call markup out, everything a person wrote left alone."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from papaya_agent_runtime import papaya_events
from papaya_agent_runtime.sanitize import FALLBACK_LINE, clean_outbound

SAMPLES = Path(__file__).parent / "samples"
CONNECTED = {
    "PAPAYA_API_URL": "https://papaya.example",
    "PAPAYA_AGENT_TOKEN": "secret-token",
    "PAPAYA_WORKSPACE_ID": "ws-1",
}

# Known vendor formats, not reproduced: built from the documented shapes.
CALL = (
    '<function_calls>\n<invoke name="Bash">\n<parameter name="command">ls</parameter>\n'
    "</invoke>\n</function_calls>"
)


@pytest.mark.parametrize("name", sorted(p.name for p in SAMPLES.glob("*.log")))
def test_real_claude_output_passes_through_unchanged(name) -> None:
    text = (SAMPLES / name).read_text()
    assert clean_outbound(text) == text


@pytest.mark.parametrize(
    "call",
    [
        CALL,
        CALL.replace("function_calls", "antml:function_calls").replace("invoke", "antml:invoke"),
        "<function_results>total 0</function_results>",
        '<invoke name="Read"><parameter name="path">a</parameter></invoke>',
        '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>',
        '<tool_use><name>Bash</name><input>{"command": "ls"}</input></tool_use>',
        '{"name": "Bash", "arguments": {"command": "ls"}}',
        '{"name": "Read", "input": {"path": "a"}}',
    ],
)
def test_each_format_is_removed_and_the_prose_around_it_kept(call) -> None:
    assert clean_outbound(f"Checking now.\n\n{call}\n\nThen I will report.") == (
        "Checking now.\n\nThen I will report."
    )


@pytest.mark.parametrize(
    "cut",
    [
        '<function_calls>\n<invoke name="Bash">\n<parameter name="command">ls',
        '<tool_call>{"name": "Bash", "argu',
        "<tool_use><name>Bash",
        '{"name": "Bash", "arguments": {"command": "l',
    ],
)
def test_a_call_cut_short_goes_to_the_end(cut) -> None:
    assert clean_outbound(f"I looked.\n\n{cut}") == "I looked."


def test_markup_in_the_middle_of_a_paragraph_leaves_the_sentence() -> None:
    text = f"I ran the check {CALL} and it passed."
    assert clean_outbound(text) == "I ran the check  and it passed."


def test_only_markup_leaves_the_fallback_line_never_empty() -> None:
    assert clean_outbound(CALL) == FALLBACK_LINE
    assert clean_outbound(f"{CALL}\n\n{CALL}") == FALLBACK_LINE
    assert clean_outbound('<tool_call>{"name": "x", "arg') == FALLBACK_LINE


def test_empty_input_stays_empty_for_the_caller_to_decide() -> None:
    assert clean_outbound("") == ""
    assert clean_outbound("  \n") == "  \n"


def test_fenced_code_is_not_altered() -> None:
    text = f"Here is what it wrote:\n\n```xml\n{CALL}\n```\n\nThat is all."
    assert clean_outbound(text) == text
    tilde = f"~~~\n{CALL}\n~~~"
    assert clean_outbound(tilde) == tilde
    unterminated = f"```\n{CALL}\nno closing fence"
    assert clean_outbound(unterminated) == unterminated


def test_markup_outside_a_fence_goes_and_the_fence_stays() -> None:
    fence = "```\n<tool_call>quoted</tool_call>\n```"
    assert clean_outbound(f"{fence}\n\n{CALL}") == fence


def test_inline_code_is_not_altered() -> None:
    text = "The runtime strips `<tool_call>` and `<function_calls>` tags."
    assert clean_outbound(text) == text
    double = 'Write ``<invoke name="x">`` to see it.'
    assert clean_outbound(double) == double


def test_json_needs_both_a_name_and_arguments_or_input() -> None:
    for kept in (
        '{"name": "Shane"}',
        '{"name": "x", "value": 1}',
        '{"arguments": {"a": 1}}',
        'The config is {"name": "x", "arguments": 1} inline.',
    ):
        assert clean_outbound(kept) == kept


def test_prose_about_tool_calls_and_other_tags_is_untouched() -> None:
    text = "I made a tool call<br>and read the <b>result</b>; <invokes> is not a tag."
    assert clean_outbound(text) == text


def test_a_stray_close_tag_or_bare_open_tag_goes_alone() -> None:
    assert clean_outbound("done </function_calls>") == "done"
    assert clean_outbound("a <tool_call> b") == "a  b"


def test_the_manager_turn_sample_with_quoted_tags_is_byte_for_byte() -> None:
    text = (SAMPLES / "claude-p-answer-quoted-tags.log").read_text()
    assert "`<tool_call>`" in text
    assert clean_outbound(text) == text


# ── one place: every write to Papaya passes through _papaya_request ────────


class _Opened:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def __call__(self, request, timeout):
        self.sink.append((request.method, json.loads(request.data or b"null"), request.full_url))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return b'{"id": "m-1", "turn_id": "t-1", "delivered_to": {}}'


def _event():
    return papaya_events.PapayaEvent(
        id="e-1", kind="work_item.comment", subject="item-9", payload={}, work_item_id="item-9"
    )


_PATHS = {
    papaya_events.REPLY_THREAD: "/api/v1/workspaces/ws-1/channels/c/messages",
    papaya_events.REPLY_DM: (
        "/api/v1/workspaces/ws-1/polyweave-agents/me/dm-conversations/c/replies"
    ),
    papaya_events.REPLY_MACHINE_TASK: "/api/v1/workspaces/ws-1/machine-tasks/t-1/reply",
}


def _reply(kind: str) -> dict:
    return {
        "kind": kind,
        "method": "POST",
        "path": _PATHS[kind],
        "parent_id": "root-1",
        "result_path": "/api/v1/workspaces/ws-1/machine-instructions/MI-1/result",
    }


def _text_of(body: dict) -> str:
    return next(body[key] for key in ("body", "text", "content", "result_summary") if key in body)


def test_every_post_function_sends_clean_text_through_the_one_request() -> None:
    sent: list = []
    opener = _Opened(sent)
    dirty = f"Done. {CALL}"
    papaya_events.post_work_item_comment(_event(), dirty, environ=CONNECTED, opener=opener)
    papaya_events.post_machine_task_reply(
        "/api/v1/workspaces/ws-1/machine-tasks/t-1/reply",
        dirty,
        papaya_events.MILESTONE_DONE,
        environ=CONNECTED,
        opener=opener,
    )
    papaya_events.post_instruction_reply(
        _reply(papaya_events.REPLY_THREAD), dirty, environ=CONNECTED, opener=opener
    )
    papaya_events.post_instruction_reply(
        _reply(papaya_events.REPLY_DM), dirty, environ=CONNECTED, opener=opener
    )
    papaya_events.post_instruction_reply(
        _reply(papaya_events.REPLY_MACHINE_TASK), dirty, environ=CONNECTED, opener=opener
    )
    papaya_events.report_instruction_result(
        _reply(papaya_events.REPLY_DM), "done", dirty, None, environ=CONNECTED, opener=opener
    )
    papaya_events.post_owner_dm(
        dirty, kind=papaya_events.OWNER_DM_NOTICE, environ=CONNECTED, opener=opener
    )
    assert len(sent) == 7
    for method, body, _url in sent:
        assert method == "POST"
        assert _text_of(body) == "Done."


def test_a_post_that_is_only_markup_says_so_in_one_line() -> None:
    sent: list = []
    papaya_events.post_work_item_comment(_event(), CALL, environ=CONNECTED, opener=_Opened(sent))
    assert sent[0][1] == {"body": FALLBACK_LINE}


def test_an_empty_post_keeps_its_own_behaviour() -> None:
    sent: list = []
    with pytest.raises(papaya_events.PapayaEventError):
        papaya_events.post_machine_task_reply(
            "/api/v1/workspaces/ws-1/machine-tasks/t-1/reply",
            "  ",
            papaya_events.MILESTONE_DONE,
            environ=CONNECTED,
            opener=_Opened(sent),
        )
    assert (
        papaya_events.post_owner_dm(
            "", kind=papaya_events.OWNER_DM_NOTICE, environ=CONNECTED, opener=_Opened(sent)
        )
        is None
    )
    assert sent == []


def test_the_status_snapshot_text_fields_are_cleaned_and_other_fields_are_not() -> None:
    sent: list = []
    snapshot = {
        "summary": f"1 task {CALL}",
        "needs_you": [{"kind": "question", "ref": "task-1", "text": f"ok {CALL}"}],
        "recently_finished": [{"ref": "task-2", "title": "T", "outcome": "closed"}],
    }
    papaya_events.put_connection_status(snapshot, environ=CONNECTED, opener=_Opened(sent))
    (_method, body, _url) = sent[0]
    assert body["summary"] == "1 task"
    assert body["needs_you"][0] == {"kind": "question", "ref": "task-1", "text": "ok"}
    assert body["recently_finished"] == snapshot["recently_finished"]


def test_reads_and_the_status_patch_are_not_touched() -> None:
    sent: list = []
    papaya_events.set_work_item_status(
        _event(), "in_progress", environ=CONNECTED, opener=_Opened(sent)
    )
    assert sent[0][1] == {"status": "in_progress"}
