"""Mechanical Papaya-event primitives stop before manager judgment."""

from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from papaya_agent_runtime import papaya_events, solicit
from papaya_agent_runtime.state import init_db, store


def _event(**payload) -> papaya_events.PapayaEvent:
    return papaya_events.PapayaEvent(
        id="event-17",
        kind="work_item.assigned",
        subject="work_item:item-9",
        payload={"work_item": {"id": "item-9", "title": "Add safe primitives"}, **payload},
        work_item_id="item-9",
    )


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_parse_event_reads_a_file_and_listener_environment_fallbacks(tmp_path) -> None:
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "id": 41,
                "kind": "work_item.assigned",
                "subject": "work_item:from-subject",
                "payload": {"note": "please take this"},
            }
        )
    )

    event = papaya_events.parse_event(
        path,
        environ={
            "PAPAYA_WORK_ITEM_ID": "from-env",
            "PAPAYA_WORKING_DIRECTORY": "/work/runtime",
        },
    )

    assert event == papaya_events.PapayaEvent(
        id="41",
        kind="work_item.assigned",
        subject="work_item:from-subject",
        payload={"note": "please take this"},
        work_item_id="from-subject",
        working_directory="/work/runtime",
    )
    from_listener = papaya_events.parse_event(environ={"PAPAYA_EVENT_FILE": str(path)})
    assert from_listener.id == "41"


def test_parse_event_accepts_stdin_and_rejects_non_objects() -> None:
    event = papaya_events.parse_event(
        stdin_text='{"payload": {}}',
        environ={
            "PAPAYA_EVENT_KIND": "mention.you",
            "PAPAYA_SUBJECT": "thread:3",
            "PAPAYA_WORK_ITEM_ID": "item-3",
        },
    )

    assert event.kind == "mention.you"
    assert event.subject == "thread:3"
    assert event.work_item_id == "item-3"
    assert papaya_events.event_key(event) == "papaya:work-item:item-3:mention.you"
    with pytest.raises(papaya_events.PapayaEventError, match="one JSON object"):
        papaya_events.parse_event(stdin_text="[]", environ={})


def test_event_key_prefers_event_id_and_refuses_unstable_identity() -> None:
    assert papaya_events.event_key(_event()) == "papaya:event:event-17"
    with pytest.raises(papaya_events.PapayaEventError, match="no stable identity"):
        papaya_events.event_key(papaya_events.PapayaEvent(None, "", "thread:x", {}))


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_hydrate_work_item_gets_and_merges_the_authoritative_record() -> None:
    event = _event()
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        return _Response(
            {
                "id": "item-9",
                "title": "Authoritative title",
                "description": "The full record.",
                "metadata": {"repository": "acme/runtime"},
            }
        )

    hydrated = papaya_events.hydrate_work_item(
        event,
        environ={
            "PAPAYA_API_URL": "https://papaya.example",
            "PAPAYA_AGENT_TOKEN": "secret-token",
            "PAPAYA_WORKSPACE_ID": "workspace/1",
        },
        opener=open_request,
    )

    assert calls[0][0].method == "GET"
    assert calls[0][1] == 15
    assert calls[0][0].full_url.endswith("/api/v1/workspaces/workspace%2F1/work-items/item-9")
    assert calls[0][0].headers["Authorization"] == "Bearer secret-token"
    assert hydrated.payload["work_item"]["title"] == "Authoritative title"
    assert papaya_events.repository_spec(hydrated) == "acme/runtime"
    assert event.payload["work_item"]["title"] == "Add safe primitives"


def test_hydrate_work_item_without_connection_facts_is_a_noop() -> None:
    event = _event()
    assert papaya_events.hydrate_work_item(event, environ={}) is event


def test_papaya_hydration_failure_is_one_actionable_line() -> None:
    def unavailable(_request, timeout):
        assert timeout == 15
        raise urllib.error.URLError("offline")

    with pytest.raises(papaya_events.PapayaEventError) as raised:
        papaya_events.hydrate_work_item(
            _event(),
            environ={
                "PAPAYA_API_URL": "https://papaya.example/api/v1",
                "PAPAYA_AGENT_TOKEN": "secret-token",
                "PAPAYA_WORKSPACE_ID": "workspace-1",
            },
            opener=unavailable,
        )
    assert "retry" in str(raised.value)
    assert "\n" not in str(raised.value)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"repo": "acme/api"}, "acme/api"),
        ({"repository": {"name": "api"}}, "api"),
        ({"repository": {"url": "https://github.com/acme/api"}}, "https://github.com/acme/api"),
        ({"work_item": {"repository": {"slug": "acme/web"}}}, "acme/web"),
        ({"work_item": {"metadata": {"repo": "acme/worker"}}}, "acme/worker"),
        ({"repository_path": "/src/api"}, "/src/api"),
    ],
)
def test_repository_spec_accepts_explicit_string_and_mapping_forms(payload, expected) -> None:
    event = papaya_events.PapayaEvent("1", "assigned", "work_item:1", payload)
    assert papaya_events.repository_spec(event) == expected


def test_repository_spec_never_falls_back_to_the_listener_working_directory(tmp_path) -> None:
    """The listener's directory is the runtime's own checkout: never a ticket's home.

    On 2026-09-16 (PAP-217) a desktop-app ticket that named no repository was
    silently placed in the runtime checkout through this fallback. An item that
    names nothing is now an error here, and a question for the brief turn.
    """
    runtime_checkout = tmp_path / "papaya-agent-runtime"
    runtime_checkout.mkdir()
    event = papaya_events.PapayaEvent(
        "1",
        "work_item.assigned",
        "work_item:1",
        {"work_item": {"id": "1", "title": "Hover card clips on the desktop app"}},
        work_item_id="1",
        working_directory=str(runtime_checkout),
    )

    with pytest.raises(papaya_events.PapayaEventError, match="does not name a repository"):
        papaya_events.repository_spec(event)


_CONNECTED = {
    "PAPAYA_API_URL": "https://papaya.example",
    "PAPAYA_AGENT_TOKEN": "secret-token",
    "PAPAYA_WORKSPACE_ID": "ws-1",
}


class _Answer:
    """A response with no body, which is what a write may legitimately return."""

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return b""


def test_set_work_item_status_patches_the_item_it_reads() -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        return _Answer()

    assert papaya_events.set_work_item_status(
        _event(), "in_progress", environ=_CONNECTED, opener=open_request
    )

    (request,) = calls
    assert request.method == "PATCH"
    assert request.full_url.endswith("/api/v1/workspaces/ws-1/work-items/item-9")
    assert json.loads(request.data) == {"status": "in_progress"}
    assert request.headers["Authorization"] == "Bearer secret-token"


def test_set_work_item_status_refuses_a_status_outside_the_runners_own() -> None:
    with pytest.raises(papaya_events.PapayaEventError, match="status must be one of"):
        papaya_events.set_work_item_status(_event(), "archived", environ=_CONNECTED)


def test_status_and_comment_without_connection_facts_make_no_call() -> None:
    def never(_request, timeout):  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("no call should be made without connection facts")

    assert papaya_events.set_work_item_status(_event(), "todo", environ={}, opener=never) is False
    assert papaya_events.post_work_item_comment(_event(), "hi", environ={}, opener=never) is False


def test_a_refused_status_change_is_one_actionable_line() -> None:
    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 409, "closed", {}, None)

    with pytest.raises(papaya_events.PapayaEventError) as raised:
        papaya_events.set_work_item_status(_event(), "review", environ=_CONNECTED, opener=refused)
    assert "status change" in str(raised.value)
    assert "HTTP 409" in str(raised.value)


def test_post_work_item_comment_posts_one_body_to_the_items_comments() -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        return _Answer()

    papaya_events.post_work_item_comment(
        _event(), "handed back: no worker; no branch", environ=_CONNECTED, opener=open_request
    )

    (request,) = calls
    assert request.method == "POST"
    assert request.full_url.endswith("/work-items/item-9/comments")
    assert json.loads(request.data) == {"body": "handed back: no worker; no branch"}


def test_ensure_repository_delegates_to_existing_boundary_without_override(monkeypatch) -> None:
    calls = []
    ensured = solicit.Ensured("runtime", "acme/runtime", True, True, "/notes")

    def ensure(spec: str, *, allow_outside: bool = False):
        calls.append((spec, allow_outside))
        return ensured

    monkeypatch.setattr(papaya_events.solicit, "ensure", ensure)

    result = papaya_events.ensure_repository(_event(repo="acme/runtime"))

    assert result is ensured
    assert calls == [("acme/runtime", False)]


def test_ensure_repository_does_not_require_or_create_acceptance_criteria(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        papaya_events.solicit,
        "ensure",
        lambda spec: (
            calls.append(spec) or solicit.Ensured("runtime", "acme/runtime", True, True, "/notes")
        ),
    )

    papaya_events.ensure_repository(_event(repo="acme/runtime"))

    assert calls == ["acme/runtime"]


def test_found_checkout_is_resolved_by_origin_and_not_modified(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "person-checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "personal-branch")
    _git(checkout, "config", "user.name", "Tester")
    _git(checkout, "config", "user.email", "tester@example.com")
    (checkout / "tracked.txt").write_text("committed\n")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "-qm", "base")
    origin = "https://github.com/acme/runtime.git"
    _git(checkout, "remote", "add", "origin", origin)
    (checkout / "tracked.txt").write_text("dirty and precious\n")
    (checkout / "untracked.txt").write_text("also precious\n")
    before = (
        _git(checkout, "rev-parse", "HEAD"),
        _git(checkout, "status", "--porcelain=v1", "--untracked-files=all"),
        (checkout / "tracked.txt").read_bytes(),
        (checkout / "untracked.txt").read_bytes(),
    )
    calls = []
    monkeypatch.setattr(
        papaya_events.solicit,
        "ensure",
        lambda spec: (
            calls.append(spec) or solicit.Ensured("runtime", "acme/runtime", True, True, "/notes")
        ),
    )

    papaya_events.ensure_repository(_event(repository_path=str(checkout)))

    after = (
        _git(checkout, "rev-parse", "HEAD"),
        _git(checkout, "status", "--porcelain=v1", "--untracked-files=all"),
        (checkout / "tracked.txt").read_bytes(),
        (checkout / "untracked.txt").read_bytes(),
    )
    assert calls == [origin]
    assert after == before


def test_recorded_event_is_findable_without_dispatching_twice(ppy_home) -> None:
    conn = init_db()
    repo_id = store.add_repo(
        conn,
        name="runtime",
        origin="https://github.com/acme/runtime",
        local_path="/runtime/repo",
        default_branch="main",
        base_sha="a" * 40,
    )
    event = _event(repo="acme/runtime")
    run_id = store.create_run(conn, "connected event")
    task_id = store.add_task(conn, run_id=run_id, title="mechanical event", repo_id=repo_id)

    papaya_events.record_task(conn, task_id, event)

    assert papaya_events.find_existing_task(conn, papaya_events.event_key(event))["id"] == task_id
    assert papaya_events.find_existing_task(conn, "papaya:event:other") is None
    assert store.get_task_env(conn, task_id, papaya_events.PAPAYA_EVENT_KEY) == (
        "papaya:event:event-17"
    )
    assert json.loads(store.get_task_env(conn, task_id, papaya_events.PAPAYA_EVENT_METADATA)) == {
        "id": "event-17",
        "kind": "work_item.assigned",
        "subject": "work_item:item-9",
        "work_item_id": "item-9",
    }


CONNECTED = {
    "PAPAYA_API_URL": "https://papaya.example",
    "PAPAYA_AGENT_TOKEN": "secret-token",
    "PAPAYA_WORKSPACE_ID": "ws-1",
}


def test_a_referenced_work_item_is_read_by_its_short_id_with_the_same_route() -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        return _Response({"id": "item-115", "short_id": "PAP-115", "status": "done"})

    record = papaya_events.read_work_item_ref("PAP-115", environ=CONNECTED, opener=open_request)
    assert record == {"id": "item-115", "short_id": "PAP-115", "status": "done"}
    assert calls[0].method == "GET"
    assert calls[0].full_url == "https://papaya.example/api/v1/workspaces/ws-1/work-items/PAP-115"
    assert papaya_events.read_work_item_ref("PAP-115", environ={}, opener=open_request) is None
    assert len(calls) == 1


def test_a_refused_reference_read_raises_with_its_code() -> None:
    def open_request(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    with pytest.raises(papaya_events.PapayaHTTPError) as raised:
        papaya_events.read_work_item_ref("JIRA-4411", environ=CONNECTED, opener=open_request)
    assert raised.value.code == 404


@pytest.mark.parametrize(
    ("item", "repo"),
    [
        ({"metadata": {"repository": "acme/web"}}, "acme/web"),
        ({"repo": {"url": "https://github.com/acme/web"}}, "https://github.com/acme/web"),
        ({"metadata": {}}, None),
        ({}, None),
    ],
)
def test_a_work_item_record_names_its_repository_only_where_a_ticket_would(item, repo) -> None:
    assert papaya_events.work_item_repository(item) == repo


def _reply(kind: str) -> dict:
    if kind == "dm":
        return {
            "kind": "agent_dm_reply",
            "method": "POST",
            "path": "/api/v1/workspaces/ws-1/polyweave-agents/me/dm-conversations/c-1/replies",
            "result_path": "/api/v1/workspaces/ws-1/machine-instructions/MI-1/result",
        }
    return {
        "kind": "thread_reply",
        "method": "POST",
        "path": "/api/v1/workspaces/ws-1/channels/chan-1/messages",
        "parent_id": "root-1",
        "result_path": "/api/v1/workspaces/ws-1/machine-instructions/MI-1/result",
    }


@pytest.mark.parametrize("origin", ["dm", "channel"])
def test_an_instruction_reply_carries_kind_only_when_given(origin) -> None:
    bodies = []

    def open_request(request, timeout):
        bodies.append(json.loads(request.data))
        return _Response({"id": "m-1", "turn_id": "t-1"})

    reply = _reply(origin)
    papaya_events.post_instruction_reply(reply, "Done.", environ=CONNECTED, opener=open_request)
    papaya_events.post_instruction_reply(
        reply, "On it.", environ=CONNECTED, opener=open_request, kind="progress"
    )
    assert "kind" not in bodies[0]
    assert bodies[1]["kind"] == "progress"


PERSON = {"type": "user", "id": "user-1", "display_name": "Shane"}


def test_follow_ups_are_read_beside_the_result_route_as_comments() -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        return _Response(
            [
                {
                    "id": "f-1",
                    "body": "actually use Postgres",
                    "author": PERSON,
                    "created_at": "t1",
                },
                {"id": "f-2", "body": "and the reports", "author": PERSON, "created_at": "t2"},
            ]
        )

    found = papaya_events.list_instruction_follow_ups(
        _reply("dm"), environ=CONNECTED, opener=open_request
    )
    assert calls[0].method == "GET"
    assert calls[0].full_url == (
        "https://papaya.example/api/v1/workspaces/ws-1/machine-instructions/MI-1/follow-ups"
        "?limit=200"
    )
    assert [(c["id"], c["body"], c["created_at"]) for c in found] == [
        ("f-1", "actually use Postgres", "t1"),
        ("f-2", "and the reports", "t2"),
    ]
    assert found[0]["author_name"] == "Shane"
    assert papaya_events.list_instruction_follow_ups(_reply("dm"), environ={}) is None


#: One follow-up exactly as Papaya serves it (`MachineInstructionFollowUpOut`, backend
#: commit 1a2aa56d6): the author is flat, and a person's `author_actor` is null.
BACKEND_FOLLOW_UP = {
    "id": "4b1f0c2e-9d7a-4e1b-8f3c-2a6d5e7f9b10",
    "body": "actually use Postgres",
    "author_type": "user",
    "author_id": "user-1",
    "author_actor": None,
    "author_display_name": "Shane",
    "origin_message_id": "msg-77",
    "created_at": "2026-09-23T10:00:05Z",
}


def test_the_backends_flat_follow_up_reads_as_a_persons_comment() -> None:
    from papaya_agent_runtime import serve

    def open_request(request, timeout):
        return _Response([BACKEND_FOLLOW_UP])

    (found,) = papaya_events.list_instruction_follow_ups(
        _reply("channel"), environ=CONNECTED, opener=open_request
    )
    assert found["id"] == BACKEND_FOLLOW_UP["id"]
    assert found["body"] == "actually use Postgres"
    assert found["created_at"] == "2026-09-23T10:00:05Z"
    assert (found["author_type"], found["author_id"], found["author_name"]) == (
        "user",
        "user-1",
        "Shane",
    )
    assert "author_actor" not in found
    assert serve.is_own_comment(found, "user-1") is False
    assert serve.is_own_comment(found, None) is False


def test_an_agents_flat_follow_up_is_still_an_agents() -> None:
    from papaya_agent_runtime import serve

    agent = papaya_events.follow_up_as_comment(
        {**BACKEND_FOLLOW_UP, "author_type": "agent", "author_id": "agent-1"}
    )
    assert serve.is_own_comment(agent, "agent-1") is True
    assert serve.is_own_comment(agent, "agent-2") is False


def test_a_page_of_follow_ups_and_an_empty_answer_read_the_same_way() -> None:
    def page(request, timeout):
        return _Response({"items": [{"id": "f-1", "body": "hi", "author": PERSON}]})

    def empty(request, timeout):
        return _Response({})

    found = papaya_events.list_instruction_follow_ups(_reply("dm"), environ=CONNECTED, opener=page)
    assert [c["id"] for c in found] == ["f-1"]
    assert (
        papaya_events.list_instruction_follow_ups(_reply("dm"), environ=CONNECTED, opener=empty)
        == []
    )


def test_a_papaya_without_the_follow_up_route_refuses_with_404() -> None:
    def open_request(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    with pytest.raises(papaya_events.PapayaHTTPError) as raised:
        papaya_events.list_instruction_follow_ups(
            _reply("channel"), environ=CONNECTED, opener=open_request
        )
    assert raised.value.code == 404


@pytest.mark.parametrize("agent_id", [None, "agent-1", "user-1"])
def test_a_persons_follow_up_is_never_taken_for_the_agents_own_comment(agent_id) -> None:
    """The one authorship rule (`serve.is_own_comment`) holds on a follow-up too."""
    from papaya_agent_runtime import serve

    person = papaya_events.follow_up_as_comment({"id": "f-1", "body": "hi", "author": PERSON})
    assert "author_actor" not in person
    assert serve.is_own_comment(person, agent_id) is False
    agent = papaya_events.follow_up_as_comment(
        {"id": "f-2", "body": "ok", "author": {"type": "agent", "id": "agent-1"}}
    )
    assert serve.is_own_comment(agent, "agent-1") is True


def test_intent_is_kept_only_when_the_event_carries_the_key() -> None:
    base = {"instruction_id": "i-1", "short_id": "MI-1", "reply": _reply("dm")}
    assert papaya_events.instruction_from(base).intent is None
    assert papaya_events.instruction_from({**base, "intent": "WORK"}).intent == "work"
    assert papaya_events.instruction_from({**base, "intent": None}).intent == ""
