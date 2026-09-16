"""Non-interactive intake prepares work without touching a person's checkout."""

from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from papaya_agent_runtime import brief_lint, intake, solicit
from papaya_agent_runtime.config import config_path
from papaya_agent_runtime.state import init_db, store


def _event(**payload) -> intake.IntakeEvent:
    return intake.IntakeEvent(
        id="event-17",
        kind="work_item.assigned",
        subject="work_item:item-9",
        payload={
            "work_item": {
                "id": "item-9",
                "title": "Add safe intake",
                "description": "Route connected work through the runtime.",
                "acceptance_criteria": "The event produces exactly one dispatched task.",
            },
            **payload,
        },
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

    event = intake.parse_event(
        path,
        environ={
            "PAPAYA_WORK_ITEM_ID": "from-env",
            "PAPAYA_WORKING_DIRECTORY": "/work/runtime",
        },
    )

    assert event == intake.IntakeEvent(
        id="41",
        kind="work_item.assigned",
        subject="work_item:from-subject",
        payload={"note": "please take this"},
        work_item_id="from-subject",
        working_directory="/work/runtime",
    )

    from_listener = intake.parse_event(
        environ={"PAPAYA_EVENT_FILE": str(path), "PAPAYA_WORKING_DIRECTORY": "/fallback"}
    )
    assert from_listener.id == "41"
    assert from_listener.working_directory == "/fallback"


def test_parse_event_accepts_stdin_and_rejects_non_objects() -> None:
    event = intake.parse_event(
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
    assert intake.event_key(event) == "papaya:work-item:item-3:mention.you"

    with pytest.raises(intake.IntakeError, match="one JSON object"):
        intake.parse_event(stdin_text="[]", environ={})


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
    event = intake.IntakeEvent("1", "assigned", "work_item:1", payload)
    assert intake.repository_spec(event) == expected


def test_explicit_repository_wins_over_listener_working_directory() -> None:
    event = _event(repo="acme/runtime")
    event = intake.IntakeEvent(**{**event.__dict__, "working_directory": "/human/checkout"})
    assert intake.repository_spec(event) == "acme/runtime"


def test_missing_definition_of_done_stops_before_repository_registration(monkeypatch) -> None:
    event = intake.IntakeEvent(
        id="4",
        kind="work_item.assigned",
        subject="work_item:4",
        payload={"repo": "acme/api", "work_item": {"title": "Unbounded work"}},
    )
    called = False

    def should_not_ensure(_spec):
        nonlocal called
        called = True

    monkeypatch.setattr(intake.solicit, "ensure", should_not_ensure)

    with pytest.raises(intake.IntakeError, match="no definition of done"):
        intake.ensure_repository(event)
    assert called is False


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_assignment_summary_is_hydrated_and_missing_done_is_written_before_work() -> None:
    event = intake.IntakeEvent(
        id="5",
        kind="work_item.assigned",
        subject="work_item:item-5",
        payload={"work_item": {"id": "item-5", "title": "Route it"}},
        work_item_id="item-5",
    )
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        if request.method == "GET":
            return _Response(
                {
                    "id": "item-5",
                    "title": "Route it",
                    "description": "Use the runtime.",
                    "metadata": {"repository": "acme/runtime"},
                    "acceptance_criteria": None,
                }
            )
        assert request.method == "PATCH"
        body = json.loads(request.data)
        assert "runtime-owned worktree" in body["acceptance_criteria"]
        return _Response(
            {
                "id": "item-5",
                "title": "Route it",
                "description": "Use the runtime.",
                "metadata": {"repository": "acme/runtime"},
                "acceptance_criteria": body["acceptance_criteria"],
            }
        )

    hydrated, wrote = intake.ensure_definition_of_done(
        event,
        environ={
            "PAPAYA_API_URL": "https://papaya.example",
            "PAPAYA_AGENT_TOKEN": "secret-token",
            "PAPAYA_WORKSPACE_ID": "workspace-1",
        },
        opener=open_request,
    )

    assert wrote is True
    assert [request.method for request, _timeout in calls] == ["GET", "PATCH"]
    assert all(timeout == 15 for _request, timeout in calls)
    assert calls[0][0].full_url.endswith("/api/v1/workspaces/workspace-1/work-items/item-5")
    assert calls[0][0].headers["Authorization"] == "Bearer secret-token"
    assert intake.repository_spec(hydrated) == "acme/runtime"
    assert "linked to its pull request" in intake.definition_of_done(hydrated)


def test_missing_done_without_papaya_write_connection_is_actionable() -> None:
    event = intake.IntakeEvent(
        id="5",
        kind="work_item.assigned",
        subject="work_item:item-5",
        payload={"work_item": {"id": "item-5", "title": "Route it"}},
        work_item_id="item-5",
    )
    with pytest.raises(intake.IntakeError, match="add acceptance criteria.*then retry"):
        intake.ensure_definition_of_done(event, environ={})


def test_papaya_hydration_failure_is_one_actionable_line() -> None:
    event = intake.IntakeEvent(
        id="5",
        kind="work_item.assigned",
        subject="work_item:item-5",
        payload={"work_item": {"id": "item-5", "title": "Route it"}},
        work_item_id="item-5",
    )

    def unavailable(_request, timeout):
        assert timeout == 15
        raise urllib.error.URLError("offline")

    with pytest.raises(intake.IntakeError) as raised:
        intake.ensure_definition_of_done(
            event,
            environ={
                "PAPAYA_API_URL": "https://papaya.example/api/v1",
                "PAPAYA_AGENT_TOKEN": "secret-token",
                "PAPAYA_WORKSPACE_ID": "workspace-1",
            },
            opener=unavailable,
        )
    assert "retry intake" in str(raised.value)
    assert "\n" not in str(raised.value)


def test_a_found_checkout_is_imported_by_origin_and_left_byte_for_byte_unchanged(
    tmp_path, monkeypatch
) -> None:
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

    before = {
        "head": _git(checkout, "rev-parse", "HEAD"),
        "branch": _git(checkout, "branch", "--show-current"),
        "status": _git(checkout, "status", "--porcelain=v1", "--untracked-files=all"),
        "tracked": (checkout / "tracked.txt").read_bytes(),
        "untracked": (checkout / "untracked.txt").read_bytes(),
    }
    calls = []
    ensured = solicit.Ensured("runtime", "acme/runtime", True, True, "/notes")

    def ensure(spec: str):
        calls.append(spec)
        return ensured

    monkeypatch.setattr(intake.solicit, "ensure", ensure)
    result = intake.ensure_repository(_event(repository_path=str(checkout)))

    after = {
        "head": _git(checkout, "rev-parse", "HEAD"),
        "branch": _git(checkout, "branch", "--show-current"),
        "status": _git(checkout, "status", "--porcelain=v1", "--untracked-files=all"),
        "tracked": (checkout / "tracked.txt").read_bytes(),
        "untracked": (checkout / "untracked.txt").read_bytes(),
    }
    assert result is ensured
    assert calls == [origin]
    assert after == before


def test_a_found_checkout_without_origin_is_an_actionable_single_line_error(
    tmp_path, monkeypatch
) -> None:
    checkout = tmp_path / "local-only"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    called = False

    def should_not_ensure(_spec):
        nonlocal called
        called = True

    monkeypatch.setattr(intake.solicit, "ensure", should_not_ensure)
    with pytest.raises(intake.IntakeError) as raised:
        intake.ensure_repository(_event(repository_path=str(checkout)))

    message = str(raised.value)
    assert "has no origin remote" in message
    assert "add an origin URL" in message
    assert "\n" not in message
    assert called is False


def test_rendered_brief_preserves_event_text_and_lints_clean_for_done() -> None:
    event = _event(repo="acme/runtime")
    brief = intake.render_brief(event)

    assert brief.startswith("# Add safe intake\n")
    assert "Route connected work through the runtime." in brief
    assert "The event produces exactly one dispatched task." in brief
    for heading in (
        "## Goals",
        "## Intent",
        "## In scope",
        "## Out of scope",
        "## Evidence contract",
        "## Gate policy",
        "## Scope-change protocol",
        "## Plan note",
        "## Closeout checklist",
    ):
        assert heading in brief
    assert brief_lint.lint_brief(brief, ends_at="done") == []


def test_defect_words_get_an_observation_section_without_inventing_a_cause() -> None:
    event = _event(
        repo="acme/runtime",
        work_item={
            "id": "item-9",
            "title": "Fix upload defect",
            "description": "The observed symptom is an uppercase FAIL result.",
            "validation_steps": ["Upload succeeds", "The regression test passes"],
        },
    )
    brief = intake.render_brief(event)
    assert "## Symptom" in brief
    assert "## Cause" not in brief
    assert brief_lint.lint_brief(brief, ends_at="done") == []


def test_event_key_prefers_event_id_and_refuses_unstable_identity() -> None:
    assert intake.event_key(_event(repo="acme/runtime")) == "papaya:event:event-17"
    with pytest.raises(intake.IntakeError, match="no stable identity"):
        intake.event_key(intake.IntakeEvent(None, "", "thread:x", {}))


def test_repeated_event_key_resolves_the_existing_task_without_dispatching_twice(
    ppy_home,
) -> None:
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
    dispatches = 0

    def intake_once() -> int:
        nonlocal dispatches
        existing = intake.find_existing_task(conn, intake.event_key(event))
        if existing is not None:
            return int(existing["id"])
        dispatches += 1
        run_id = store.create_run(conn, "connected event")
        task_id = store.add_task(conn, run_id=run_id, title="intake", repo_id=repo_id)
        intake.record_task(conn, task_id, event)
        return task_id

    first = intake_once()
    second = intake_once()
    assert second == first
    assert dispatches == 1
    assert intake.find_existing_task(conn, "papaya:event:event-1") is None
    assert store.get_task_env(conn, first, intake.INTAKE_EVENT_KEY) == intake.event_key(event)
    assert json.loads(store.get_task_env(conn, first, intake.INTAKE_EVENT_METADATA)) == {
        "id": "event-17",
        "kind": "work_item.assigned",
        "subject": "work_item:item-9",
        "work_item_id": "item-9",
    }


def test_ensure_supervisor_starts_a_detached_owner_and_waits_for_its_socket(
    ppy_home, monkeypatch
) -> None:
    from papaya_agent_runtime.supervisor import client as client_module

    class FakeClient:
        pings = 0

        def ping(self):
            self.pings += 1
            if self.pings == 1:
                raise client_module.SupervisorUnavailable("not up")
            return {"ok": True}

    launched = []

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return object()

    monkeypatch.setattr(client_module, "SupervisorClient", FakeClient)
    monkeypatch.setattr(client_module.subprocess, "Popen", popen)

    client, started = client_module.ensure_supervisor(timeout=1)

    assert isinstance(client, FakeClient)
    assert started is True
    assert launched[0][0][1:] == ["-m", "papaya_agent_runtime", "supervisor", "serve"]
    assert launched[0][1]["start_new_session"] is True
    assert (ppy_home / "run" / "supervisor.log").exists()


def test_cli_repeating_the_same_event_does_not_dispatch_twice(
    ppy_home, tmp_path, monkeypatch, capsys
) -> None:
    from papaya_agent_runtime import health, preflight, readiness
    from papaya_agent_runtime.cli import main
    from papaya_agent_runtime.supervisor import client as client_module

    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "id": "event-17",
                "kind": "work_item.assigned",
                "subject": "work_item:item-9",
                "payload": {
                    "repo": "acme/runtime",
                    "work_item": {
                        "id": "item-9",
                        "title": "Add safe intake",
                        "description": "Route the event through the runtime.",
                        "acceptance_criteria": "Exactly one task is dispatched.",
                    },
                },
            }
        )
    )
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("present")
    conn = init_db()
    repo_id = store.add_repo(
        conn,
        name="runtime",
        origin="https://github.com/acme/runtime",
        local_path="/runtime/repo",
        default_branch="main",
        base_sha="a" * 40,
    )
    dispatches = []

    class FakeClient:
        def dispatch_task(self, **kwargs):
            dispatches.append(kwargs)
            run_id = store.create_run(conn, "intake")
            task_id = store.add_task(conn, run_id=run_id, title=kwargs["title"], repo_id=repo_id)
            store.set_task_env(
                conn,
                task_id,
                intake.INTAKE_EVENT_KEY,
                kwargs["intake_event_key"],
                source="intake",
            )
            return {"ok": True, "run_id": run_id, "task_id": task_id, "branch": "task"}

    monkeypatch.setattr(client_module, "ensure_supervisor", lambda: (FakeClient(), True))
    monkeypatch.setattr(
        intake,
        "ensure_repository",
        lambda _event: solicit.Ensured("runtime", "acme/runtime", True, True, "/notes"),
    )
    monkeypatch.setattr(readiness, "check", lambda: readiness.Readiness())
    monkeypatch.setattr(health, "require_dispatch_capacity", lambda _repo: None)
    monkeypatch.setattr(preflight, "check_disk", lambda: 10.0)

    assert main(["intake", str(event_path)]) == 0
    assert main(["intake", str(event_path)]) == 0

    assert len(dispatches) == 1
    output = capsys.readouterr().out
    assert "dispatched task" in output
    assert "event already dispatched as task" in output
