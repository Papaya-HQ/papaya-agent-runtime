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
