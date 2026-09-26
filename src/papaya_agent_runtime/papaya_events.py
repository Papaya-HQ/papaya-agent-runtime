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
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from papaya_agent_runtime import repos, sanitize, solicit
from papaya_agent_runtime.state import store

PAPAYA_EVENT_KEY = "papaya_event_key"
PAPAYA_EVENT_METADATA = "papaya_event_metadata"
#: A ticket task's work item as a person names it: its display id (`PAP-231`) and
#: title, recorded once when the ticket is taken so `ppy workers` can say it.
WORK_ITEM_KEY = "papaya_work_item_key"
WORK_ITEM_TITLE = "papaya_work_item_title"
#: The work item's link in the app, when its event carried one: the status
#: snapshot's `about.url` (the ledger has no other way to build an app URL).
WORK_ITEM_URL = "papaya_work_item_url"
_URL_KEYS = ("url", "web_url", "app_url", "html_url")
#: Where a work item carries its display id: `WorkItemOut.short_id` on the full
#: record, `display_id` on an event summary, then older names.
_DISPLAY_ID_KEYS = ("short_id", "display_id", "key", "identifier", "ticket_key")
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


class PapayaHTTPError(PapayaEventError):
    """Papaya answered with an HTTP error: its status and the body it said it in.

    Still a :class:`PapayaEventError` with the same message, so every caller that
    reads the sentence keeps working; the code and ``detail`` are for a caller that
    has to tell a 422 on one field from a 409 with a reason (the status snapshot,
    an instruction's result).
    """

    def __init__(self, message: str, *, code: int, detail: Any = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.detail = detail

    @property
    def reason(self) -> str:
        """The `detail.reason` Papaya's 409/403 bodies carry, or ``""``."""
        detail = self.detail.get("detail") if isinstance(self.detail, dict) else None
        return str(detail.get("reason") or "") if isinstance(detail, dict) else ""

    def fields(self) -> list[str]:
        """The fields a 422 names (`detail[].loc`, joined with dots), in order."""
        detail = self.detail.get("detail") if isinstance(self.detail, dict) else None
        found: list[str] = []
        for entry in detail if isinstance(detail, list) else []:
            loc = entry.get("loc") if isinstance(entry, dict) else None
            if isinstance(loc, list | tuple):
                found.append(".".join(str(part) for part in loc if part != "body"))
        return [name for name in found if name]


#: The subject kinds Papaya reserves (`SUBJECT_KINDS` on the backend) that this
#: runtime takes: a work item, and an instruction a person sent to this machine.
SUBJECT_WORK_ITEM = "work_item"
SUBJECT_INSTRUCTION = "instruction"
SUBJECT_KINDS = (SUBJECT_WORK_ITEM, SUBJECT_INSTRUCTION)
#: The event kind of an instruction a person sent to their own machine.
MACHINE_INSTRUCTION = "machine.instruction"


def subject_parts(subject: str) -> tuple[str, str] | None:
    """``(kind, id)`` for a subject of a kind in :data:`SUBJECT_KINDS`, else ``None``."""
    kind, sep, ident = str(subject or "").strip().partition(":")
    if not sep or kind not in SUBJECT_KINDS or not ident.strip():
        return None
    return kind, ident.strip()


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


#: The methods that carry words a person reads.
_WRITES = ("POST", "PATCH", "PUT")
#: The body fields a person reads: a comment, a reply, an instruction's result, and
#: the text fields of the status snapshot (which nests them in its lists).
_PERSON_FIELDS = frozenset(
    {"body", "text", "content", "result_summary", "summary", "title", "how", "outcome"}
)


def _for_a_person(value: Any) -> Any:
    """``value`` with every field a person reads cleaned of tool-call markup.

    The one place every write to Papaya passes through (`_papaya_request`), so a new
    posting path cannot skip it.
    """
    if isinstance(value, Mapping):
        return {
            key: sanitize.clean_outbound(item)
            if key in _PERSON_FIELDS and isinstance(item, str)
            else _for_a_person(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_for_a_person(item) for item in value]
    return value


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
        if method in _WRITES:
            body = _for_a_person(body)
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
        raise PapayaHTTPError(
            f"Papaya refused the work-item {what} (HTTP {exc.code}); "
            "refresh this agent connection and retry",
            code=exc.code,
            detail=_error_body(exc),
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


def _error_body(exc: urllib.error.HTTPError) -> Any:
    """The JSON an HTTP error carried, or ``None``; never raises."""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - a body we cannot read is simply not there
        return None
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


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


def read_work_item(
    event: PapayaEvent,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> dict[str, Any] | None:
    """Papaya's current record of this event's work item: its status and owner now.

    ``None`` when there is nothing to call with (not connected), which a caller must
    read as "cannot tell". A refusal or an unreachable Papaya raises
    :class:`PapayaEventError`, like every other read here.
    """
    env = os.environ if environ is None else environ
    url = _papaya_work_item_url(event, env)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return None
    return _papaya_request(url, token, what="read", opener=opener)


def read_work_item_ref(
    ref: str,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> dict[str, Any] | None:
    """Papaya's record of the work item ``ref`` names: its short id (`PAP-115`) or UUID.

    The work-item read takes either (the backend's `WorkItemPathRef`), so an
    instruction's reference is read with the same route and token a ticket's is.
    ``None`` when not connected; a refusal raises :class:`PapayaHTTPError` (a 404 is
    an id this workspace does not have, e.g. another tracker's).
    """
    env = os.environ if environ is None else environ
    api_url = _clean(env.get(_PAPAYA_API_ENV))
    workspace = _clean(env.get(_PAPAYA_WORKSPACE_ENV))
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if not api_url or not workspace or not token or not str(ref or "").strip():
        return None
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    url = (
        f"{base}/workspaces/{urllib.parse.quote(workspace, safe='')}"
        f"/work-items/{urllib.parse.quote(str(ref).strip(), safe='')}"
    )
    return _papaya_request(url, token, what="read", opener=opener)


def work_item_repository(item: Mapping[str, Any]) -> str | None:
    """The repository a work item record names (its fields or metadata), or ``None``."""
    try:
        event = PapayaEvent(id=None, kind="", subject="", payload={"work_item": dict(item)})
        return repository_spec(event)
    except PapayaEventError:
        return None


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
    mentions: list[Mapping[str, Any]] | None = None,
) -> bool:
    """Post one comment on this event's work item. Returns whether a call was made.

    ``mentions`` are real mention payloads (``type``, ``id``, ``handle``,
    ``display_name``), carried in the comment's ``metadata`` the way the app's own
    comments carry them, so the person is notified rather than merely named.

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
        body={"body": text, "metadata": {"mentions": list(mentions)}}
        if mentions
        else {"body": text},
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


def read_agent_record(
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> dict[str, Any] | None:
    """Papaya's record of the agent a job's token speaks for, or ``None``.

    The agent context read (`GET .../polyweave-agents/me/context`) is the one route an
    agent token can call that carries the agent's ``ownership_scope``. ``None`` when
    there is nothing to call with; a refusal or an unreachable Papaya raises
    :class:`PapayaEventError` like every other read here.
    """
    env = os.environ if environ is None else environ
    api_url = _clean(env.get(_PAPAYA_API_ENV))
    workspace = _clean(env.get(_PAPAYA_WORKSPACE_ENV))
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if not api_url or not workspace or not token:
        return None
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    url = (
        f"{base}/workspaces/{urllib.parse.quote(workspace, safe='')}/polyweave-agents/me/context"
        "?max_records=1&include_sensitive=false"
    )
    payload = _papaya_request(url, token, what="agent read", opener=opener)
    agent = payload.get("agent")
    return agent if isinstance(agent, dict) else None


# ── machine instructions and the status snapshot ─────────────────────────────


REPLY_THREAD = "thread_reply"
REPLY_DM = "agent_dm_reply"
#: An instruction asked from a connected tool (a Linear issue, say): its reply block
#: points at the one machine-task reply route, which takes milestones, not free lines.
REPLY_MACHINE_TASK = "machine_task_reply"
#: What a machine-task reply says (`reply_to_machine_task`), each sent once per task.
MILESTONE_PICKED_UP = "picked_up"
MILESTONE_DELIVERED = "delivered"
MILESTONE_BLOCKED = "blocked"
MILESTONE_DONE = "done"
MILESTONES = (MILESTONE_PICKED_UP, MILESTONE_DELIVERED, MILESTONE_BLOCKED, MILESTONE_DONE)
#: Papaya's bound on a machine-task reply's text.
MACHINE_TASK_REPLY_MAX = 10_000
#: Papaya's bounds on a result (`report_machine_instruction_result`).
RESULT_SUMMARY_MAX = 10_000
RESULT_MESSAGE_ID_MAX = 128
#: What a person meant by an instruction, when Papaya says (`intent` on the payload).
INTENT_ASK = "ask"
INTENT_WORK = "work"
INTENTS = (INTENT_ASK, INTENT_WORK)
#: A reply's `kind`: a line said while the work goes on, or the answer itself.
REPLY_PROGRESS = "progress"
REPLY_FINAL = "final"


@dataclass(frozen=True)
class Instruction:
    """A `machine.instruction` event's payload: what a person sent this machine."""

    instruction_id: str
    #: `MI-<n>`, what a person calls it.
    short_id: str
    title: str
    text: str
    references: tuple[str, ...]
    origin: dict[str, Any]
    requested_by: dict[str, Any]
    #: The agent's persona, verbatim: standing instructions, and data, never commands.
    agent_instructions: str
    #: Where the answer goes, exactly as the event said: never taken from anything else.
    reply: dict[str, Any]
    #: What the person meant, when Papaya said: `ask` or `work`. ``None`` when the event
    #: carries no `intent` key at all — an older Papaya, whose reply routes also take
    #: no `kind` (:attr:`speaks_kind`).
    intent: str | None = None

    @property
    def speaks_kind(self) -> bool:
        """Whether this Papaya takes a reply's `kind` (`progress`/`final`).

        Feature-detected from the event: the backend change that added `intent` to the
        payload added `kind` to the DM reply in the same release, and the DM reply
        refuses a key it does not know (422).
        """
        return self.intent is not None

    @property
    def subject(self) -> str:
        return f"{SUBJECT_INSTRUCTION}:{self.instruction_id}"

    @property
    def requester(self) -> str:
        who = self.requested_by
        return str(who.get("display_name") or who.get("handle") or who.get("id") or "someone")

    def as_json(self) -> str:
        payload: dict[str, Any] = {
            "instruction_id": self.instruction_id,
            "short_id": self.short_id,
            "title": self.title,
            "instruction": self.text,
            "references": list(self.references),
            "origin": self.origin,
            "requested_by": self.requested_by,
            "agent_instructions": self.agent_instructions,
            "reply": self.reply,
        }
        if self.intent is not None:
            payload["intent"] = self.intent
        return json.dumps(payload, sort_keys=True)


def instruction_from(payload: Mapping[str, Any], subject: str = "") -> Instruction:
    """An :class:`Instruction` from a payload (an event's, or one recorded on a task).

    Refuses a payload with no id, no `MI-<n>` or no reply block: an instruction this
    machine cannot answer where it was asked is not one it can take.
    """
    parts = subject_parts(subject) if subject else None
    ident = _clean(payload.get("instruction_id")) or (parts[1] if parts else None)
    short_id = _clean(payload.get("short_id"))
    reply = payload.get("reply")
    if not ident or not short_id or not isinstance(reply, Mapping):
        raise PapayaEventError(
            "a machine instruction needs instruction_id, short_id and a reply block"
        )
    references = payload.get("references")
    origin = payload.get("origin")
    who = payload.get("requested_by")
    intent: str | None = None
    if "intent" in payload:
        # Present means this Papaya knows the key; a value it does not name is no intent.
        intent = str(payload.get("intent") or "").strip().lower()
        intent = intent if intent in INTENTS else ""
    return Instruction(
        instruction_id=ident,
        short_id=short_id,
        title=str(payload.get("title") or "").strip(),
        text=str(payload.get("instruction") or ""),
        references=tuple(str(r) for r in references if str(r).strip())
        if isinstance(references, list)
        else (),
        origin=dict(origin) if isinstance(origin, Mapping) else {},
        requested_by=dict(who) if isinstance(who, Mapping) else {},
        agent_instructions=str(payload.get("agent_instructions") or ""),
        reply=dict(reply),
        intent=intent,
    )


def parse_instruction(event: PapayaEvent) -> Instruction:
    """The instruction a `machine.instruction` event carries, or refuse."""
    parts = subject_parts(event.subject)
    if event.kind != MACHINE_INSTRUCTION or parts is None or parts[0] != SUBJECT_INSTRUCTION:
        raise PapayaEventError(
            f"not a machine instruction: kind {event.kind!r}, subject {event.subject!r}"
        )
    return instruction_from(event.payload, event.subject)


def _workspace_path(environ: Mapping[str, str]) -> str:
    workspace = _clean(environ.get(_PAPAYA_WORKSPACE_ENV))
    return re.escape(workspace) if workspace else "[^/]+"


def machine_task_reply_path(path: object, environ: Mapping[str, str]) -> str:
    """A machine task's reply route, checked against this workspace, or refuse."""
    found = str(path or "")
    ws = _workspace_path(environ)
    if not re.fullmatch(rf"/api/v1/workspaces/{ws}/machine-tasks/[^/]+/reply", found):
        raise PapayaEventError("a machine task's reply path is not in this workspace")
    return found


def post_machine_task_reply(
    path: str,
    text: str,
    milestone: str,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> dict[str, Any] | None:
    """Send one milestone where a machine task was asked (`reply_to_machine_task`).

    Returns Papaya's answer (`status`, `delivered_to`, `replayed`, ...), or ``None``
    when there is nothing to call with (not connected). Papaya sends each milestone
    once per task: a resend answers ``replayed: true`` and posts nothing. A refusal
    raises :class:`PapayaHTTPError`.
    """
    if milestone not in MILESTONES:
        raise PapayaEventError(f"a machine task has no milestone {milestone!r}")
    line = str(text or "").strip()[:MACHINE_TASK_REPLY_MAX]
    if not line:
        raise PapayaEventError("a machine task reply needs text")
    env = os.environ if environ is None else environ
    url = _api_url(env, machine_task_reply_path(path, env))
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return None
    return _papaya_request(
        url,
        token,
        method="POST",
        body={"text": line, "milestone": milestone},
        what="machine task reply",
        opener=opener,
    )


def reply_paths(reply: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[str, str]:
    """The reply block's ``(path, result_path)``, checked against this workspace.

    Only the shapes the wire names, in this connection's workspace: a path
    anywhere else is not somewhere this machine was asked, whoever wrote it.
    """
    ws = _workspace_path(environ)
    kind = str(reply.get("kind") or "")
    path = str(reply.get("path") or "")
    result_path = str(reply.get("result_path") or "")
    shapes = {
        REPLY_THREAD: rf"/api/v1/workspaces/{ws}/channels/[^/]+/messages",
        REPLY_DM: rf"/api/v1/workspaces/{ws}/polyweave-agents/me/dm-conversations/[^/]+/replies",
        REPLY_MACHINE_TASK: rf"/api/v1/workspaces/{ws}/machine-tasks/[^/]+/reply",
    }
    shape = shapes.get(kind)
    if shape is None or str(reply.get("method") or "POST").upper() != "POST":
        raise PapayaEventError(f"an instruction's reply block has an unknown kind {kind!r}")
    if not re.fullmatch(shape, path):
        raise PapayaEventError("an instruction's reply path is not in this workspace")
    if not re.fullmatch(rf"/api/v1/workspaces/{ws}/machine-instructions/[^/]+/result", result_path):
        raise PapayaEventError("an instruction's result path is not in this workspace")
    return path, result_path


def _api_url(environ: Mapping[str, str], path: str) -> str | None:
    """``path`` (`/api/v1/...`) on this connection's Papaya, or ``None`` when unknown."""
    api_url = _clean(environ.get(_PAPAYA_API_ENV))
    if not api_url:
        return None
    base = api_url.rstrip("/")
    base = base.removesuffix("/api/v1")
    return base + path


def post_instruction_reply(
    reply: Mapping[str, Any],
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
    kind: str | None = None,
    milestone: str | None = None,
) -> str | None:
    """Answer an instruction where it was asked. Returns the posted message's id.

    A channel origin posts ``{"content", "parent_id"}`` and answers with the
    message's ``id``; a DM posts ``{"text"}`` and answers with the ``turn_id`` the
    result route takes as ``result_message_id``. ``None`` when there is nothing to
    call with (not connected). A refusal raises :class:`PapayaHTTPError`.

    ``kind`` (`progress` or `final`) is sent only when given, and a caller gives it
    only when the event said this Papaya takes it (:attr:`Instruction.speaks_kind`):
    an older DM route refuses the key outright.

    An instruction asked from a connected tool answers through the machine-task
    route, which takes one reply per ``milestone``: the final answer is ``done``, and
    a progress line reaches it only when it is one (``milestone``) — any other line
    is not sent and answers ``None``.
    """
    env = os.environ if environ is None else environ
    path, _result = reply_paths(reply, env)
    if reply.get("kind") == REPLY_MACHINE_TASK:
        if milestone is None and kind != REPLY_PROGRESS:
            milestone = MILESTONE_DONE
        if milestone is None:
            return None
        sent = post_machine_task_reply(path, text, milestone, environ=env, opener=opener)
        if sent is None:
            return None
        where = sent.get("delivered_to")
        ref = where.get("ref") if isinstance(where, dict) else None
        found = ref.get("message_id") if isinstance(ref, dict) else None
        return str(found) if found else ""
    url = _api_url(env, path)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return None
    body: dict[str, Any]
    if reply.get("kind") == REPLY_THREAD:
        body = {"content": text, "parent_id": reply.get("parent_id")}
    else:
        body = {"text": text}
    if kind is not None:
        body["kind"] = kind
    answer = _papaya_request(
        url, token, method="POST", body=body, what="instruction reply", opener=opener
    )
    key = "id" if reply.get("kind") == REPLY_THREAD else "turn_id"
    found = answer.get(key)
    if found is None and isinstance(answer.get("message"), dict):
        found = answer["message"].get("id")
    return str(found) if found is not None else ""


def report_instruction_result(
    reply: Mapping[str, Any],
    status: str,
    summary: str,
    message_id: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> bool:
    """Report an instruction's outcome to its ``result_path``. Returns whether a call was made."""
    if status not in ("done", "failed"):
        raise PapayaEventError(f"an instruction's result is done or failed, not {status!r}")
    env = os.environ if environ is None else environ
    _path, result_path = reply_paths(reply, env)
    url = _api_url(env, result_path)
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return False
    body: dict[str, Any] = {"status": status, "result_summary": str(summary)[:RESULT_SUMMARY_MAX]}
    if message_id:
        body["result_message_id"] = str(message_id)[:RESULT_MESSAGE_ID_MAX]
    _papaya_request(url, token, method="POST", body=body, what="instruction result", opener=opener)
    return True


#: How many follow-ups one read asks for: the route's ceiling (its default is 100).
FOLLOW_UPS_LIMIT = 200


def follow_up_as_comment(follow_up: Mapping[str, Any]) -> dict[str, Any]:
    """A follow-up in the shape a comment has, from Papaya's follow-up record.

    Papaya's shape is flat (`MachineInstructionFollowUpOut`): `{id, body, author_type,
    author_id, author_actor, author_display_name, origin_message_id, created_at}`, a
    person's with `author_type: "user"` and `author_actor: null`. A nested
    `author: {type, id, display_name}` is read as a fallback.

    So the comment cursor, dedupe and authorship rule apply unchanged. A person's
    follow-up never carries `author_actor`: that key alone makes a comment an agent's
    (`sweep.is_agent_comment`), and an agent's comment is never woken for.
    """
    author = follow_up.get("author")
    who: Mapping[str, Any] = author if isinstance(author, Mapping) else {}
    kind = (
        str(follow_up.get("author_type") or who.get("type") or who.get("kind") or "")
        .strip()
        .lower()
    )
    name = (
        _clean(follow_up.get("author_display_name"))
        or _clean(who.get("display_name"))
        or _clean(who.get("name"))
        or _clean(who.get("handle"))
        or (_clean(author) if isinstance(author, str) else None)
    )
    comment: dict[str, Any] = {
        "id": follow_up.get("id"),
        "body": str(follow_up.get("body") or ""),
        "created_at": follow_up.get("created_at"),
        "author_type": "agent" if kind == "agent" else (kind or "user"),
        "author_id": _clean(follow_up.get("author_id")) or _clean(who.get("id")),
        "author_name": name,
    }
    if kind == "agent":
        actor = follow_up.get("author_actor")
        comment["author_actor"] = (
            dict(actor) if isinstance(actor, Mapping) and actor else {"name": name or "agent"}
        )
    return comment


def list_instruction_follow_ups(
    reply: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> list[dict[str, Any]] | None:
    """What the person added to an instruction since sending it, oldest first, as comments.

    Read beside the instruction's result route (`.../machine-instructions/<ref>/follow-ups`),
    checked against this workspace the same way. ``None`` when there is nothing to call
    with (not connected). A refusal raises :class:`PapayaHTTPError` — a 404 is a Papaya
    that has no follow-ups yet, which the caller reads as none.
    """
    env = os.environ if environ is None else environ
    _path, result_path = reply_paths(reply, env)
    url = _api_url(env, result_path.removesuffix("/result") + "/follow-ups")
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return None
    answer = _papaya_request(
        f"{url}?limit={FOLLOW_UPS_LIMIT}",
        token,
        what="follow-up list",
        opener=opener,
        shape=object,
    )
    if isinstance(answer, Mapping):
        # A page rather than a bare list; an empty body is no follow-ups.
        answer = answer.get("items", answer.get("follow_ups", [])) if answer else []
    if not isinstance(answer, list):
        raise PapayaEventError("Papaya returned an invalid follow-up list; retry")
    return [follow_up_as_comment(item) for item in answer if isinstance(item, Mapping)]


#: An instruction Papaya still has open: offered to a machine, or held by one.
INSTRUCTION_OPEN = ("routed", "picked_up")


def read_instruction_status(
    reply: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> str | None:
    """Where Papaya has an instruction now: `routed`, `picked_up`, `done`, `failed`,
    `not_picked_up` or `cancelled` (`GET .../machine-instructions/<ref>`).

    The route is the one beside its result route, checked against this workspace the
    same way. ``None`` when there is nothing to call with (not connected). A refusal,
    or an answer with no status, raises :class:`PapayaEventError`.
    """
    env = os.environ if environ is None else environ
    _path, result_path = reply_paths(reply, env)
    url = _api_url(env, result_path.removesuffix("/result"))
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if url is None or token is None:
        return None
    answer = _papaya_request(url, token, what="instruction read", opener=opener)
    status = str(answer.get("status") or "").strip().lower()
    if not status:
        raise PapayaEventError("Papaya returned an instruction with no status; retry")
    return status


def put_connection_status(
    snapshot: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> bool:
    """PUT this machine's status snapshot. Returns whether a call was made.

    `PUT .../polyweave-agents/me/connection/status` with the connection's own token:
    the token is the connection, so nothing in the path or body names it.
    """
    env = os.environ if environ is None else environ
    workspace = _clean(env.get(_PAPAYA_WORKSPACE_ENV))
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    if not workspace or not token:
        return False
    url = _api_url(
        env,
        f"/api/v1/workspaces/{urllib.parse.quote(workspace, safe='')}"
        "/polyweave-agents/me/connection/status",
    )
    if url is None:
        return False
    _papaya_request(url, token, method="PUT", body=snapshot, what="status snapshot", opener=opener)
    return True


#: What a message to the owner is (`OwnerDmMessage.kind`): telling them, or asking them.
OWNER_DM_NOTICE = "notice"
OWNER_DM_QUESTION = "question"
#: The route's limits: `body` 1-4000 characters, `dedupe_key` 1-128.
OWNER_DM_MAX_CHARS = 4000
OWNER_DM_KEY_MAX = 128


def post_owner_dm(
    body: str,
    *,
    kind: str,
    dedupe_key: str | None = None,
    environ: Mapping[str, str] | None = None,
    opener=urllib.request.urlopen,
) -> dict[str, Any] | None:
    """Say ``body`` to the person who connected this machine, in their agent DM.

    `POST .../polyweave-agents/me/owner-dm/messages` with the connection's own token
    (backend PR #1042): the token is the connection and its owner, so nothing in the
    request names a person or a conversation. The same ``dedupe_key`` within 24 hours
    posts nothing and answers 200 with ``replayed: true``. Returns the answer, or
    ``None`` when there is nothing to call with (not connected). A refusal raises
    :class:`PapayaHTTPError`: a 404 is a Papaya that predates the route.
    """
    if kind not in (OWNER_DM_NOTICE, OWNER_DM_QUESTION):
        raise PapayaEventError(f"a message to the owner is a notice or a question, not {kind!r}")
    env = os.environ if environ is None else environ
    workspace = _clean(env.get(_PAPAYA_WORKSPACE_ENV))
    token = _clean(env.get(_PAPAYA_TOKEN_ENV))
    text = str(body or "").strip()
    if not workspace or not token or not text:
        return None
    url = _api_url(
        env,
        f"/api/v1/workspaces/{urllib.parse.quote(workspace, safe='')}"
        "/polyweave-agents/me/owner-dm/messages",
    )
    if url is None:
        return None
    if len(text) > OWNER_DM_MAX_CHARS:
        text = text[: OWNER_DM_MAX_CHARS - 1].rstrip() + "…"
    payload: dict[str, Any] = {"body": text, "kind": kind}
    if dedupe_key:
        payload["dedupe_key"] = str(dedupe_key)[:OWNER_DM_KEY_MAX]
    return _papaya_request(url, token, method="POST", body=payload, what="owner DM", opener=opener)


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
    return ensure_spec(repository_spec(event))


def ensure_spec(spec: str) -> solicit.Ensured:
    """Register ``spec`` (a URL, slug, or checkout whose origin is read) through `solicit`."""
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


def work_item_label(event: PapayaEvent) -> tuple[str, str]:
    """The work item's display id (`PAP-231`) and title, each `""` when the event lacks it."""
    item = event.payload.get("work_item")
    if not isinstance(item, dict):
        return "", ""
    key = next(
        (str(item[name]).strip() for name in _DISPLAY_ID_KEYS if str(item.get(name) or "").strip()),
        "",
    )
    return key, str(item.get("title") or "").strip()


def record_work_item_label(conn: sqlite3.Connection, task_id: int, event: PapayaEvent) -> None:
    """Record the item's display id and title on its ticket task, once.

    A value already there stays: the id never changes, and the title a ticket was
    taken under is the one its work was briefed against.
    """
    key, title = work_item_label(event)
    item = event.payload.get("work_item")
    url = ""
    if isinstance(item, dict):
        url = next(
            (
                str(item[name]).strip()
                for name in _URL_KEYS
                if str(item.get(name) or "").strip().startswith(("http://", "https://"))
            ),
            "",
        )
    for name, value in ((WORK_ITEM_KEY, key), (WORK_ITEM_TITLE, title), (WORK_ITEM_URL, url)):
        if value and not store.get_task_env(conn, task_id, name):
            store.set_task_env(conn, task_id, name, value, source="papaya_event")


__all__ = [
    "MACHINE_INSTRUCTION",
    "PAPAYA_EVENT_KEY",
    "PAPAYA_EVENT_METADATA",
    "REPLY_DM",
    "REPLY_MACHINE_TASK",
    "REPLY_THREAD",
    "MACHINE_TASK_REPLY_MAX",
    "MILESTONES",
    "MILESTONE_BLOCKED",
    "MILESTONE_DELIVERED",
    "MILESTONE_DONE",
    "MILESTONE_PICKED_UP",
    "machine_task_reply_path",
    "post_machine_task_reply",
    "SUBJECT_INSTRUCTION",
    "SUBJECT_KINDS",
    "SUBJECT_WORK_ITEM",
    "WORK_ITEM_KEY",
    "WORK_ITEM_TITLE",
    "WORK_ITEM_URL",
    "INTENTS",
    "INTENT_ASK",
    "INTENT_WORK",
    "REPLY_FINAL",
    "REPLY_PROGRESS",
    "Instruction",
    "PapayaHTTPError",
    "FOLLOW_UPS_LIMIT",
    "follow_up_as_comment",
    "instruction_from",
    "list_instruction_follow_ups",
    "read_instruction_status",
    "INSTRUCTION_OPEN",
    "read_work_item_ref",
    "work_item_repository",
    "parse_instruction",
    "OWNER_DM_KEY_MAX",
    "OWNER_DM_MAX_CHARS",
    "OWNER_DM_NOTICE",
    "OWNER_DM_QUESTION",
    "post_instruction_reply",
    "post_owner_dm",
    "put_connection_status",
    "reply_paths",
    "report_instruction_result",
    "subject_parts",
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
    "record_work_item_label",
    "repository_spec",
    "set_work_item_status",
    "work_item_label",
]
