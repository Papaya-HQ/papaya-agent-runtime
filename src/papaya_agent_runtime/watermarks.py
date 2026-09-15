"""Comment-sweep watermarks for external records the harness does not own.

During the Radar QA loop the manager swept six tickets every ten minutes with a
subagent that re-read every comment — about 65k tokens a sweep — and found a new
comment on well under one sweep in five (issue #62). ``ppy watch`` keeps per-PR
state so a tick asks the forge only what changed; this is the same shape for a
ticket, a thread, or any record with a timestamp: the newest one the manager has
processed, keyed by the record's URL or id. A sweep asks only for what is newer
than the watermark and advances it once the new comments are handled.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from papaya_agent_runtime.state import store


class WatermarkError(ValueError):
    """A watermark that cannot be recorded, with the reason in plain words."""


def parse_timestamp(raw: str) -> datetime:
    """An ISO-8601 timestamp as an aware UTC datetime; refuses anything else.

    A trailing ``Z`` is accepted. A timestamp with no zone is taken as UTC, since
    that is what every forge and ticket API hands back.
    """
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WatermarkError(
            f"not an ISO-8601 timestamp: {raw!r} — pass the newest comment's own "
            "timestamp, for example 2026-09-06T14:03:00Z"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize(raw: str) -> str:
    """The stored form of a timestamp: ISO-8601, UTC, ``+00:00`` suffix."""
    return parse_timestamp(raw).isoformat()


def set_watermark(
    conn: sqlite3.Connection, key: str, raw: str, *, note: str | None = None
) -> tuple[str, str | None]:
    """Record ``raw`` as the newest processed timestamp on ``key``.

    Returns the stored timestamp and the previous one (None when the key was
    unset). Moving a watermark backwards is allowed — it is how a manager asks
    for a re-read — and the caller can say so, since the previous value comes back.
    """
    key = key.strip()
    if not key:
        raise WatermarkError("a watermark needs a record key: the ticket URL or id")
    stored = normalize(raw)
    previous = store.get_watermark(conn, key)
    store.set_watermark(conn, key, stored, note=note)
    return stored, previous["watermark"] if previous else None


def get_watermark(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return store.get_watermark(conn, key.strip())


def list_watermarks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return store.list_watermarks(conn)


def delete_watermark(conn: sqlite3.Connection, key: str) -> bool:
    return store.delete_watermark(conn, key.strip())
