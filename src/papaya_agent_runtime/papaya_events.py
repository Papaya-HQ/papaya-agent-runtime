"""Mechanical primitives for Papaya work events.

This module parses listener envelopes, fetches their authoritative work item,
resolves explicitly named repositories through the runtime's existing
registration boundary, and records event identity for idempotency.  It does
not choose work, write briefs, define acceptance criteria, or dispatch tasks.

A checkout supplied by an event is identity evidence only.  Its only use is
reading ``origin``; :func:`solicit.ensure` receives that URL and selects or
creates the runtime-owned clone.
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

PAPAYA_EVENT_KEY = "papaya_event_key"
PAPAYA_EVENT_METADATA = "papaya_event_metadata"
#: Exported by `ppy serve` into a manager turn: the run of the ticket it holds.
#: `ppy dispatch` files a worker under it when no `--run-id` is given, and a
#: worker in that run is how the runner knows the ticket was dispatched.
TICKET_RUN_ENV = "PPY_TICKET_RUN_ID"
_REPOSITORY_KEYS = ("repo", "repository", "repository_url", "repository_path")
_REPOSITORY_VALUE_KEYS = ("url", "path", "slug", "name")
_PAPAYA_API_ENV = "PAPAYA_API_URL"
_PAPAYA_TOKEN_ENV = "PAPAYA_AGENT_TOKEN"
_PAPAYA_WORKSPACE_ENV = "PAPAYA_WORKSPACE_ID"


class PapayaEventError(RuntimeError):
    """A Papaya event primitive needs an actionable correction."""


@dataclass(frozen=True)
class PapayaEvent:
    """The stable portion of a Papaya listener envelope used by the runtime."""

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
) -> PapayaEvent:
    """Read and normalize one Papaya listener JSON object.

    The caller owns reading real stdin; accepting text here keeps parsing
    deterministic and directly testable.  Listener environment values are
    fallbacks, not overrides of facts present in the envelope.
    """
    env = os.environ if environ is None else environ
    if source is None and stdin_text is None:
        source = _clean(env.get("PAPAYA_EVENT_FILE"))
    if source is not None and stdin_text is not None:
        raise PapayaEventError("accept either an event file or stdin, not both")
    if source is None and stdin_text is None:
        raise PapayaEventError("no event supplied; provide a JSON event file or stdin")

    if source is not None:
        try:
            raw = Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            raise PapayaEventError(f"cannot read event file {source}: {_one_line(exc)}") from exc
    else:
        raw = stdin_text or ""

    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PapayaEventError(f"event is not valid JSON: {_one_line(exc)}") from exc
    if not isinstance(decoded, dict):
        raise PapayaEventError("event must be one JSON object")

    payload = decoded.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise PapayaEventError("event payload must be a JSON object")

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

    return PapayaEvent(
        id=_clean(decoded.get("id")),
        kind=kind,
        subject=subject,
        payload=dict(payload),
        work_item_id=work_item_id,
        working_directory=_clean(env.get("PAPAYA_WORKING_DIRECTORY")),
    )


def event_key(event: PapayaEvent) -> str:
    """Return the stable identity that makes retrying one event harmless."""
    if event.id:
        return f"papaya:event:{event.id}"
    if event.work_item_id and event.kind:
        return f"papaya:work-item:{event.work_item_id}:{event.kind}"
    raise PapayaEventError(
        "event has no stable identity; include its id, or both PAPAYA_WORK_ITEM_ID and kind"
    )


def _work_item(event: PapayaEvent) -> Mapping[str, Any]:
    value = event.payload.get("work_item")
    return value if isinstance(value, Mapping) else {}


def _papaya_work_item_url(event: PapayaEvent, environ: Mapping[str, str]) -> str | None:
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
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
    what: str = "read",
    opener=urllib.request.urlopen,
    shape: type = dict,
) -> Any:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(dict(body)).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with opener(request, timeout=15) as response:
            # A write may legitimately answer 204 with no body; a read may not,
            # and the caller is the one that knows which it asked for.
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raise PapayaEventError(
            f"Papaya refused the work-item {what} (HTTP {exc.code}); "
            "refresh this agent connection and retry"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PapayaEventError(
            f"Papaya could not be reached to {what} the work item: {_one_line(exc)}; retry"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PapayaEventError(f"Papaya returned an unreadable work-item {what}; retry") from exc
    if not isinstance(payload, shape) and not (shape is list and payload == {}):
        raise PapayaEventError(f"Papaya returned an invalid work-item {what}; retry")
    return [] if shape is list and payload == {} else payload


def _with_work_item(event: PapayaEvent, work_item: Mapping[str, Any]) -> PapayaEvent:
    payload = dict(event.payload)
    existing = _work_item(event)
    payload["work_item"] = {**existing, **dict(work_item)}
    return replace(event, payload=payload)


def hydrate_work_item(
    event: PapayaEvent,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> PapayaEvent:
    """Fetch and merge the full work item when Papaya connection facts are present."""
    env = os.environ if environ is None else environ
    url = _papaya_work_item_url(event, env)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return event
    return _with_work_item(event, _papaya_request(url, token, what="read", opener=opener))


#: The work-item statuses `ppy serve` sets while it holds a ticket. Status is
#: *state*, not judgment: each of these follows mechanically from where the work
#: has got to, and anything said in words on the item is a manager turn's to say.
STATUS_TODO = "todo"
STATUS_IN_PROGRESS = "in_progress"
STATUS_REVIEW = "review"
STATUS_BLOCKED = "blocked"
#: Set once, when the rounds observe the ticket's pull request merged.
STATUS_DONE = "done"
WORK_ITEM_STATUSES = (STATUS_TODO, STATUS_IN_PROGRESS, STATUS_REVIEW, STATUS_BLOCKED, STATUS_DONE)


def set_work_item_status(
    event: PapayaEvent,
    status: str,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> bool:
    """Move this event's work item to ``status``. Returns whether a call was made.

    ``False`` means there was nothing to call with — no API url, no workspace, no
    token, no work item — which is the ordinary state of a machine that is not
    connected and never an error. A route that *is* reachable and refuses raises
    :class:`PapayaEventError`, because that is a fact the caller should report.
    """
    if status not in WORK_ITEM_STATUSES:
        raise PapayaEventError(
            f"status must be one of {', '.join(WORK_ITEM_STATUSES)}, not {status!r}"
        )
    env = os.environ if environ is None else environ
    url = _papaya_work_item_url(event, env)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return False
    _papaya_request(
        url,
        token,
        method="PATCH",
        body={"status": status},
        what="status change",
        opener=opener,
    )
    return True


def post_work_item_comment(
    event: PapayaEvent,
    body: str,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> bool:
    """Post one comment on this event's work item. Returns whether a call was made.

    For the runner's single mechanical sentence only — the hand-back line. What a
    manager has to *say* about a ticket is said by a manager turn through MCP.
    Same connection rules as :func:`set_work_item_status`.
    """
    text = str(body or "").strip()
    if not text:
        raise PapayaEventError("a comment needs a body")
    env = os.environ if environ is None else environ
    url = _papaya_work_item_url(event, env)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return False
    _papaya_request(
        f"{url}/comments",
        token,
        method="POST",
        body={"body": text},
        what="comment",
        opener=opener,
    )
    return True


def list_work_item_comments(
    event: PapayaEvent,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> list[dict[str, Any]] | None:
    """Every comment on this event's work item, oldest first.

    ``None`` when there is nothing to call with (not connected), which a caller
    must read as "cannot tell" rather than as "no comments". Same connection
    rules as :func:`set_work_item_status`.
    """
    env = os.environ if environ is None else environ
    url = _papaya_work_item_url(event, env)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return None
    comments = _papaya_request(
        f"{url}/comments", token, what="comment list", opener=opener, shape=list
    )
    return [comment for comment in comments if isinstance(comment, dict)]


def _repository_value(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key in _REPOSITORY_VALUE_KEYS:
            found = _clean(value.get(key))
            if found:
                return found
        return None
    return _clean(value)


def repository_spec(event: PapayaEvent) -> str:
    """Return the repository this event names, or refuse.

    There is deliberately no fallback to the listener's working directory. That
    directory is the *runtime's own checkout* — the one place a ticket can never
    belong — and on the first real run (2026-09-16, PAP-217, a desktop-app ticket)
    the fallback silently placed the ticket there. An event that names nothing is
    a question for the manager's brief turn, which has six ordered ways to answer
    it and a person to ask when none of them do; a mechanical guess is not one.
    """
    item = _work_item(event)
    metadata = item.get("metadata")
    containers = (event.payload, item, metadata if isinstance(metadata, Mapping) else {})
    for container in containers:
        for key in _REPOSITORY_KEYS:
            found = _repository_value(container.get(key))
            if found:
                return found
    raise PapayaEventError(
        "event does not name a repository; add repo, repository, repository_url, "
        "or repository_path to the work item"
    )


def ensure_repository(event: PapayaEvent) -> solicit.Ensured:
    """Resolve and onboard the named repo through :func:`solicit.ensure`.

    No override is passed: repositories outside the user's account and
    organizations still require the existing explicit ``allow_outside`` path.
    """
    spec = repository_spec(event)
    path = Path(spec).expanduser()
    if path.is_dir():
        origin = repos.remote_url(str(path))
        if not origin:
            raise PapayaEventError(
                f"cannot import checkout {path}: it has no origin remote; add an origin URL "
                "that the runtime can register and deliver through"
            )
        spec = origin
    try:
        return solicit.ensure(spec)
    except (solicit.SolicitError, repos.RepoError) as exc:
        raise PapayaEventError(_one_line(exc)) from exc


def event_metadata(event: PapayaEvent) -> dict[str, str]:
    """Return compact, non-secret event provenance for a task record."""
    values = {
        "id": event.id,
        "kind": event.kind,
        "subject": event.subject,
        "work_item_id": event.work_item_id,
    }
    return {key: value for key, value in values.items() if value}


def find_existing_task(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """Find the task previously recorded for exactly ``key``, if any."""
    return conn.execute(
        """
        SELECT tasks.*
        FROM tasks
        JOIN task_env ON task_env.task_id = tasks.id
        WHERE task_env.key = ? AND task_env.value = ?
        ORDER BY tasks.id
        LIMIT 1
        """,
        (PAPAYA_EVENT_KEY, key),
    ).fetchone()


def record_task(conn: sqlite3.Connection, task_id: int, event: PapayaEvent) -> None:
    """Bind a task to its event key and compact event provenance."""
    store.set_task_env(conn, task_id, PAPAYA_EVENT_KEY, event_key(event), source="papaya_event")
    store.set_task_env(
        conn,
        task_id,
        PAPAYA_EVENT_METADATA,
        json.dumps(event_metadata(event), sort_keys=True, separators=(",", ":")),
        source="papaya_event",
    )


__all__ = [
    "PAPAYA_EVENT_KEY",
    "PAPAYA_EVENT_METADATA",
    "STATUS_BLOCKED",
    "STATUS_IN_PROGRESS",
    "STATUS_REVIEW",
    "STATUS_TODO",
    "TICKET_RUN_ENV",
    "WORK_ITEM_STATUSES",
    "PapayaEvent",
    "PapayaEventError",
    "ensure_repository",
    "event_key",
    "event_metadata",
    "find_existing_task",
    "hydrate_work_item",
    "list_work_item_comments",
    "parse_event",
    "post_work_item_comment",
    "record_task",
    "repository_spec",
    "set_work_item_status",
]
