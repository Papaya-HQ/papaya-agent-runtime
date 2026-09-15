"""Which record a task belongs to, in whatever tracker this workspace uses.

Papaya work items are the default, not the assumption. A workspace that has said
it tracks work in Linear, Notion, Jira or anything else is telling every agent
something true, and a runtime that hard-codes one tracker quietly overrides it —
first in the pull request bodies it writes, then in the habits it teaches.

So this module knows exactly two things: that a task may belong to some record
somewhere, and how to say that in a sentence. It never talks to a tracker. Creating
records, moving them, commenting on them — all of that is the agent's job through
whatever tools the workspace actually has connected, and the runtime has no opinion
about which those are.

**Where the opinion lives.** Not here, and not in local config: the workspace states
its tracker as durable knowledge in Papaya, which is injected into every connected
agent's context at the start of a session. A new machine picks it up on its first
turn with nothing to configure. That makes it a *preference* rather than a
guarantee — an agent could still reach for the wrong tool — which is the tradeoff
Shane chose on 2026-09-15 over enforcing it at the tool gate.
"""

from __future__ import annotations

#: Task-env keys recording the tracker record a task belongs to.
PROVIDER_KEY = "tracker_provider"
RECORD_KEY = "tracker_record"
URL_KEY = "tracker_url"
TITLE_KEY = "tracker_title"

#: What a workspace gets when nobody has said otherwise. Papaya is the default
#: because it is the one tracker every connected agent already has; it is not a
#: preference, and a workspace that named another tracker always wins.
DEFAULT_PROVIDER = "papaya"

#: How a provider's name reads in a sentence. Deliberately not a closed set: a
#: workspace may track work somewhere neither this code nor the user has named,
#: and an unknown provider must render as itself rather than be refused.
_LABELS = {
    "papaya": "Papaya",
    "linear": "Linear",
    "notion": "Notion",
    "jira": "Jira",
    "github": "GitHub",
    "gitlab": "GitLab",
    "asana": "Asana",
    "shortcut": "Shortcut",
    "height": "Height",
}


def label(provider: str) -> str:
    """The tracker's name as a person writes it, for anything unknown too."""
    cleaned = (provider or "").strip()
    if not cleaned:
        return _LABELS[DEFAULT_PROVIDER]
    return _LABELS.get(cleaned.lower(), cleaned)


def link_task(
    conn,
    task_id: int,
    *,
    record: str,
    provider: str = DEFAULT_PROVIDER,
    url: str = "",
    title: str = "",
) -> None:
    """Record which tracker record a dispatched task belongs to.

    Not every task earns a record — most are a step inside one, and minting a
    ticket per step is the noise this runtime exists to avoid. The link exists so
    the tasks that *do* belong to tracked work carry it into the pull request body,
    instead of the connection living only in the session's head.
    """
    from papaya_agent_runtime.state import store

    store.set_task_env(conn, task_id, RECORD_KEY, record, source="tracker")
    store.set_task_env(conn, task_id, PROVIDER_KEY, (provider or DEFAULT_PROVIDER).strip().lower())
    if url:
        store.set_task_env(conn, task_id, URL_KEY, url, source="tracker")
    if title:
        store.set_task_env(conn, task_id, TITLE_KEY, title, source="tracker")


def task_link(conn, task_id: int) -> dict | None:
    """The tracker record a task belongs to, or None when it is unlinked."""
    from papaya_agent_runtime.state import store

    record = store.get_task_env(conn, task_id, RECORD_KEY)
    if not record:
        return None
    return {
        "record": record,
        "provider": store.get_task_env(conn, task_id, PROVIDER_KEY) or DEFAULT_PROVIDER,
        "url": store.get_task_env(conn, task_id, URL_KEY) or "",
        "title": store.get_task_env(conn, task_id, TITLE_KEY) or "",
    }


def link_sentence(link: dict | None) -> str:
    """How a tracker link reads in a pull request body or a report.

    Describes the record rather than citing a bare identifier, because a reader
    outside this workspace cannot look one up — and names the tracker, because a
    reviewer who wants to find it needs to know where to look.
    """
    if not link:
        return ""
    where = label(link.get("provider") or DEFAULT_PROVIDER)
    title = link.get("title") or ""
    record = link.get("record") or ""
    url = link.get("url") or ""
    if title:
        subject = f'"{title}"'
    elif record:
        subject = record
    else:
        subject = "its tracked record"
    if url:
        return f"Tracked in {where} under {subject} ({url})."
    return f"Tracked in {where} under {subject}."


__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDER_KEY",
    "RECORD_KEY",
    "TITLE_KEY",
    "URL_KEY",
    "label",
    "link_sentence",
    "link_task",
    "task_link",
]
