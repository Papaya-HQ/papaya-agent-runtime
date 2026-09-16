"""Deterministic preparation for one Papaya event.

This module deliberately stops before dispatch.  It turns the listener's JSON
envelope into a validated event, resolves the named repository through the
normal solicitation boundary, renders a worker brief, and provides the small
task-env primitives a caller needs for idempotency.

A checkout supplied by the event is evidence about *which* repository the work
belongs to, never a place to work.  Its only use here is reading ``origin``;
``solicit.ensure`` always receives that URL and therefore creates or selects the
runtime-owned clone.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from papaya_agent_runtime import repos, solicit
from papaya_agent_runtime.state import store

INTAKE_EVENT_KEY = "intake_event_key"
INTAKE_EVENT_METADATA = "intake_event_metadata"
_REPOSITORY_KEYS = ("repo", "repository", "repository_url", "repository_path")
_REPOSITORY_VALUE_KEYS = ("url", "path", "slug", "name")
_DONE_KEYS = ("definition_of_done", "acceptance_criteria", "validation_steps")
_PAPAYA_API_ENV = "PAPAYA_API_URL"
_PAPAYA_TOKEN_ENV = "PAPAYA_AGENT_TOKEN"
_PAPAYA_WORKSPACE_ENV = "PAPAYA_WORKSPACE_ID"


class IntakeError(RuntimeError):
    """An event cannot be prepared without a person's actionable correction."""


@dataclass(frozen=True)
class IntakeEvent:
    """The stable portion of a Papaya listener envelope used by intake."""

    id: str | None
    kind: str
    subject: str
    payload: dict[str, Any]
    work_item_id: str | None = None
    working_directory: str | None = None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def parse_event(
    source: str | Path | None = None,
    *,
    stdin_text: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> IntakeEvent:
    """Read one JSON object from ``source`` or supplied stdin text.

    The CLI owns reading real stdin; accepting its text here keeps parsing
    deterministic and directly testable.  Listener environment values are
    fallbacks, not overrides of facts present in the envelope.
    """
    env = os.environ if environ is None else environ
    if source is None and stdin_text is None:
        source = _clean(env.get("PAPAYA_EVENT_FILE"))
    if source is not None and stdin_text is not None:
        raise IntakeError("intake accepts either an event file or stdin, not both")
    if source is None and stdin_text is None:
        raise IntakeError("no event supplied; pass a JSON event file or pipe one to stdin")

    if source is not None:
        try:
            raw = Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            raise IntakeError(f"cannot read event file {source}: {_one_line(exc)}") from exc
    else:
        raw = stdin_text or ""

    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise IntakeError(f"event is not valid JSON: {_one_line(exc)}") from exc
    if not isinstance(decoded, dict):
        raise IntakeError("event must be one JSON object")

    payload = decoded.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise IntakeError("event payload must be a JSON object")

    subject = _clean(decoded.get("subject")) or _clean(env.get("PAPAYA_SUBJECT")) or ""
    kind = _clean(decoded.get("kind")) or _clean(env.get("PAPAYA_EVENT_KIND")) or ""
    work_item = payload.get("work_item")
    work_item_id = None
    if isinstance(work_item, Mapping):
        work_item_id = _clean(work_item.get("id"))
    if work_item_id is None and subject.startswith("work_item:"):
        work_item_id = _clean(subject.partition(":")[2])
    work_item_id = work_item_id or _clean(decoded.get("work_item_id"))
    work_item_id = work_item_id or _clean(env.get("PAPAYA_WORK_ITEM_ID"))

    return IntakeEvent(
        id=_clean(decoded.get("id")),
        kind=kind,
        subject=subject,
        payload=dict(payload),
        work_item_id=work_item_id,
        working_directory=_clean(env.get("PAPAYA_WORKING_DIRECTORY")),
    )


def event_key(event: IntakeEvent) -> str:
    """Return the identity used to make retries of one listener event harmless."""
    if event.id:
        return f"papaya:event:{event.id}"
    if event.work_item_id and event.kind:
        return f"papaya:work-item:{event.work_item_id}:{event.kind}"
    raise IntakeError(
        "event has no stable identity; include its id, or both PAPAYA_WORK_ITEM_ID and kind"
    )


def _work_item(event: IntakeEvent) -> Mapping[str, Any]:
    value = event.payload.get("work_item")
    return value if isinstance(value, Mapping) else {}


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple)):
        return bool(value)
    return True


def definition_of_done(event: IntakeEvent) -> object:
    """Return the event's acceptance material, refusing work that has none."""
    for container in (event.payload, _work_item(event)):
        for key in _DONE_KEYS:
            value = container.get(key)
            if _present(value):
                return value
    record = event.work_item_id or event.subject or event.id or "this event"
    raise IntakeError(
        f"{record} has no definition of done; add definition_of_done, "
        "acceptance_criteria, or validation_steps to the work item before intake"
    )


def _papaya_work_item_url(event: IntakeEvent, environ: Mapping[str, str]) -> str | None:
    api_url = _clean(environ.get(_PAPAYA_API_ENV))
    workspace = _clean(environ.get(_PAPAYA_WORKSPACE_ENV))
    if not api_url or not workspace or not event.work_item_id:
        return None
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    workspace_path = urllib.parse.quote(workspace, safe="")
    item_path = urllib.parse.quote(event.work_item_id, safe="")
    return f"{base}/workspaces/{workspace_path}/work-items/{item_path}"


def _papaya_request(
    url: str,
    token: str,
    *,
    method: str,
    body: dict[str, object] | None = None,
    opener=urllib.request.urlopen,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise IntakeError(
            f"Papaya refused the work-item {method.lower()} (HTTP {exc.code}); "
            "refresh this agent connection and retry intake"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise IntakeError(
            f"Papaya could not be reached to read the work item: {_one_line(exc)}; retry intake"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntakeError("Papaya returned an unreadable work item; retry intake") from exc
    if not isinstance(payload, dict):
        raise IntakeError("Papaya returned an invalid work item; retry intake")
    return payload


def _with_work_item(event: IntakeEvent, work_item: Mapping[str, Any]) -> IntakeEvent:
    payload = dict(event.payload)
    existing = _work_item(event)
    payload["work_item"] = {**existing, **dict(work_item)}
    return replace(event, payload=payload)


def hydrate_work_item(
    event: IntakeEvent,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> IntakeEvent:
    """Fetch the full tracked record when the listener supplied only its summary.

    Papaya assignment envelopes intentionally stay small: their work-item block
    names the item but omits its description, acceptance criteria, and metadata.
    Intake needs those before it can choose a repository or brief a worker, so it
    reads the authoritative record with the same agent token the listener passed.
    """
    env = os.environ if environ is None else environ
    url = _papaya_work_item_url(event, env)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return event
    return _with_work_item(event, _papaya_request(url, token, method="GET", opener=opener))


def _generated_definition_of_done(event: IntakeEvent) -> str:
    title = _title(event)
    return "\n".join(
        (
            f'- The requested outcome in "{title}" is implemented in the named repository.',
            "- The repository's documented local verification gate passes in the "
            "runtime-owned worktree.",
            "- The exact delivered commit is reviewed through Papaya Agent Runtime and "
            "linked to its pull request.",
        )
    )


def ensure_definition_of_done(
    event: IntakeEvent,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> tuple[IntakeEvent, bool]:
    """Hydrate the record and write acceptance criteria before any work starts.

    Returns the enriched event and whether this call added the criteria.  The
    generated criteria are deliberately outcome-level: intake has not onboarded
    the repository yet and must not invent its command or implementation shape.
    """
    env = os.environ if environ is None else environ
    hydrated = hydrate_work_item(event, environ=env, opener=opener)
    try:
        definition_of_done(hydrated)
    except IntakeError:
        pass
    else:
        return hydrated, False

    url = _papaya_work_item_url(hydrated, env)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        record = hydrated.work_item_id or hydrated.subject or "the work item"
        raise IntakeError(
            f"{record} has no definition of done and this intake has no Papaya write "
            "connection; add acceptance criteria to the work item, then retry"
        )
    criteria = _generated_definition_of_done(hydrated)
    updated = _papaya_request(
        url,
        token,
        method="PATCH",
        body={"acceptance_criteria": criteria},
        opener=opener,
    )
    enriched = _with_work_item(hydrated, updated)
    definition_of_done(enriched)
    return enriched, True


def _repository_value(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key in _REPOSITORY_VALUE_KEYS:
            found = _clean(value.get(key))
            if found:
                return found
        return None
    return _clean(value)


def repository_spec(event: IntakeEvent) -> str:
    """Return the explicitly named repository, then the listener cwd fallback."""
    item = _work_item(event)
    metadata = item.get("metadata")
    containers = (event.payload, item, metadata if isinstance(metadata, Mapping) else {})
    for container in containers:
        for key in _REPOSITORY_KEYS:
            found = _repository_value(container.get(key))
            if found:
                return found
    if event.working_directory:
        return event.working_directory
    raise IntakeError(
        "event does not name a repository; add repo, repository, repository_url, "
        "or repository_path to the work item"
    )


def ensure_repository(event: IntakeEvent) -> solicit.Ensured:
    """Resolve and onboard the event's repo without ever working in a found checkout."""
    # This check intentionally precedes even repository discovery/registration:
    # a tracked record needs acceptance criteria before work starts.
    definition_of_done(event)
    spec = repository_spec(event)
    path = Path(spec).expanduser()
    if path.is_dir():
        origin = repos.remote_url(str(path))
        if not origin:
            raise IntakeError(
                f"cannot import checkout {path}: it has no origin remote; add an origin URL "
                "that the runtime can register and deliver through"
            )
        spec = origin
    try:
        return solicit.ensure(spec)
    except (solicit.SolicitError, repos.RepoError) as exc:
        raise IntakeError(_one_line(exc)) from exc


def _human_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return "\n".join(f"- {key}: {_human_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {_human_text(item)}" for item in value)
    return str(value)


def _title(event: IntakeEvent) -> str:
    item = _work_item(event)
    title = _clean(item.get("title")) or _clean(event.payload.get("title"))
    return title or event.subject or event.kind or "Papaya work item"


def _description(event: IntakeEvent) -> str:
    item = _work_item(event)
    for container in (item, event.payload):
        for key in ("description", "text", "note", "summary"):
            value = _clean(container.get(key))
            if value:
                return value
    return "Complete the requested work item and deliver its recorded definition of done."


def render_brief(event: IntakeEvent) -> str:
    """Render a self-contained brief that passes the runtime's done-phase lint."""
    done = _human_text(definition_of_done(event))
    title = _title(event)
    description = _description(event)
    repository = repository_spec(event)

    # A brief containing defect vocabulary is subject to the defect lint.  The
    # event itself is the observed evidence available at intake, so reproduce it
    # as the symptom without asserting a cause.
    defect_text = "\n".join((title, description, done))
    symptom = ""
    from papaya_agent_runtime import brief_lint

    if brief_lint.is_defect_brief(defect_text):
        symptom = f"\n## Symptom\n\nThe incoming work item reports:\n\n{description}\n"

    return f"""# {title}
{symptom}
## Goals

Complete the work item **{title}** in `{repository}`.

### Definition of done

{done}

## Intent

{description}

## In scope

- Changes required to satisfy the recorded definition of done in the registered repository.
- Focused tests and documentation directly required by those changes.

## Out of scope

- Changes unrelated to the recorded work item or its definition of done.
- Editing any checkout discovered outside the runtime-owned repository clone.

## Evidence contract

Keep durable command output and other requested receipts in the repository's configured evidence
directory, and name those paths in the final progress report.

## Gate policy

Use the registered repository's onboarded local gate as the authoritative verification suite.
Do not substitute a broader suite owned by CI.

## Scope-change protocol

Stop and report a conflict before widening beyond the In scope section. Record any approved change
before implementing it.

## Plan note

Before editing, report a concise plan that maps the definition of done to the intended changes and
tests.

## Closeout checklist

- The recorded definition of done is satisfied.
- The authoritative local gate passes.
- The exact final diff has been reviewed.
- Final progress reports both outside-scope requirements and anything flagged but not done.
"""


def event_metadata(event: IntakeEvent) -> dict[str, str]:
    """Small, non-secret provenance recorded with the dispatched task."""
    values = {
        "id": event.id,
        "kind": event.kind,
        "subject": event.subject,
        "work_item_id": event.work_item_id,
    }
    return {key: value for key, value in values.items() if value}


def find_existing_task(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """Find the task previously dispatched for exactly ``key``, if any."""
    return conn.execute(
        """
        SELECT tasks.*
        FROM tasks
        JOIN task_env ON task_env.task_id = tasks.id
        WHERE task_env.key = ? AND task_env.value = ?
        ORDER BY tasks.id
        LIMIT 1
        """,
        (INTAKE_EVENT_KEY, key),
    ).fetchone()


def record_task(conn: sqlite3.Connection, task_id: int, event: IntakeEvent) -> None:
    """Bind a dispatched task to its event key and compact event provenance."""
    store.set_task_env(conn, task_id, INTAKE_EVENT_KEY, event_key(event), source="intake")
    store.set_task_env(
        conn,
        task_id,
        INTAKE_EVENT_METADATA,
        json.dumps(event_metadata(event), sort_keys=True, separators=(",", ":")),
        source="intake",
    )
