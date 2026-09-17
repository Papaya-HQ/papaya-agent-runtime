"""How long things take in each repository, and how long the runtime waits because of it.

Shane, 2026-09-16: "the runtime should be able to adjust per-repo timeouts based on
observed behaviour; a broad default is going to consistently fail us." One default fits
no repository: the backend suite is longer than the harness's ten-minute tool cap, a
frontend worker's first ten minutes are an install that reads as silence, and a client
gate is forty seconds, so a thirty-minute threshold there is thirty minutes of nothing.

The runtime already sees every one of those durations as it happens. So each is kept,
where it ends, as one row in ``repo_observations`` (repository, kind, seconds, when,
task, outcome), and every wait that used to read a global default reads the budget for
its repository instead:

- **budget** = the 90th percentile (nearest rank) of the window × 1.5, floored at the
  configured default (for ``plan``, the smaller of the default and the window's
  shortest observation) and capped at :data:`CAPS`;
- **window** = the newest :data:`WINDOW_COUNT` observations within
  :data:`WINDOW_DAYS` days, whichever is fewer, leaving out anything flagged
  ``stall`` or ``kill`` — a worker that stalled is what the silence budget protects
  against, not an example of normal silence;
- with fewer than :data:`MIN_OBSERVATIONS` in the window the default applies, and the
  budget says so;
- a person's override (``ppy repo set <name> --budget <kind>=<seconds>``) wins over both.

``ppy repo budgets [<repo>]`` prints the derivation, so it can be trusted or overridden.
Writing an observation never raises: a duration the runtime could not keep is a
lost data point, never a failed gate or a stopped worker.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: A local gate run (`ppy gate run`).
GATE = "gate"
#: A full-suite run (`ppy gate run --full`, or a pre-push hook that runs the full suite).
FULL_SUITE = "full_suite"
#: A worker session, from its runner starting to its process exiting.
WORKER_SESSION = "worker_session"
#: A worker's plan phase: dispatch to its first progress note that is not `plan`.
PLAN = "plan"
#: The interval between two of a worker's progress notes (the first: since dispatch).
SILENCE = "silence"
#: A manager brief turn under `ppy serve`.
BRIEF_TURN = "brief_turn"
#: A manager review turn under `ppy serve`.
REVIEW_TURN = "review_turn"
#: A delivered pull request's CI, first check started to last check finished.
CI = "ci"

KINDS = (GATE, FULL_SUITE, WORKER_SESSION, PLAN, SILENCE, BRIEF_TURN, REVIEW_TURN, CI)

#: A gate's peak resident memory, in megabytes, summed over its process group.
#: Kept in the same table (the ``seconds`` column holds the megabytes) but never a time
#: budget: it has no default, no override and no factor, only a p90 a heavy gate waits
#: for as free memory before it starts.
GATE_MEMORY = "gate_memory"
MEASURES = (GATE_MEMORY,)

#: An observation of something that stalled: kept, never derived from.
STALL = "stall"
#: An observation of something killed before it could finish: kept, never derived from.
KILL = "kill"
EXCLUDED_OUTCOMES = (STALL, KILL)

WINDOW_COUNT = 20
WINDOW_DAYS = 14
MIN_OBSERVATIONS = 3
PERCENTILE = 0.9
FACTOR = 1.5

#: The harness's cap on one tool call. A gate budget past it means `ppy gate run` is
#: the only way that gate finishes.
TOOL_CAP_SECONDS = 600.0

#: The most any derivation may say, however slow the history.
CAPS: dict[str, float] = {
    GATE: 2 * 3600.0,
    FULL_SUITE: 2 * 3600.0,
    WORKER_SESSION: 6 * 3600.0,
    PLAN: 45 * 60.0,
    SILENCE: 90 * 60.0,
    BRIEF_TURN: 2 * 3600.0,
    REVIEW_TURN: 2 * 3600.0,
    CI: 2 * 3600.0,
}

#: Defaults for the kinds the configuration has no word on.
FIXED_DEFAULTS: dict[str, float] = {
    GATE: TOOL_CAP_SECONDS,
    FULL_SUITE: TOOL_CAP_SECONDS,
    BRIEF_TURN: 30 * 60.0,
    REVIEW_TURN: 30 * 60.0,
    CI: 30 * 60.0,
}

DERIVED = "derived"
DEFAULT = "default"
OVERRIDE = "override"


class BudgetError(Exception):
    """A budget override was refused."""


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def default_seconds(kind: str) -> float:
    """The configured default a budget is floored at, and falls back to without history.

    Silence and plan are ``health.quiet_minutes`` and ``health.plan_minutes``; a worker
    session is twice ``health.checkin_after``, so its midpoint is the check-in the rounds
    always did. The rest are fixed.
    """
    if kind not in KINDS:
        raise BudgetError(f"unknown budget kind {kind!r}; one of {', '.join(KINDS)}")
    if kind in FIXED_DEFAULTS:
        return FIXED_DEFAULTS[kind]
    from papaya_agent_runtime.config import HealthPolicy, load_config

    try:
        policy = load_config().health
    except Exception:  # noqa: BLE001 - budgets are read before setup too
        policy = HealthPolicy()
    if kind == SILENCE:
        return float(policy.quiet_minutes) * 60
    if kind == PLAN:
        return float(policy.plan_minutes) * 60
    return float(policy.checkin_after) * 60 * 2


def percentile(values: Iterable[float], fraction: float = PERCENTILE) -> float | None:
    """The nearest-rank percentile: the smallest value at least ``fraction`` of them reach."""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


# ── writing ─────────────────────────────────────────────────────────────────


def observe(
    repo: str | None,
    kind: str,
    seconds: float,
    *,
    task_id: int | None = None,
    outcome: str = "",
    at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Keep one duration (or measure). Returns whether it was kept; never raises."""
    if not repo or (kind not in KINDS and kind not in MEASURES):
        return False
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value) or value < 0:
        return False
    own = conn is None
    try:
        if own:
            from papaya_agent_runtime.state import init_db

            conn = init_db()
        conn.execute(
            "INSERT INTO repo_observations (repo, kind, seconds, at, task_id, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(repo), kind, round(value, 3), _stamp(at or _now()), task_id, outcome or ""),
        )
        conn.commit()
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        if own and conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def repo_of_task(conn: sqlite3.Connection, task_id: int | None) -> str | None:
    """The registered repository a task works in, by name."""
    if task_id is None:
        return None
    row = conn.execute(
        "SELECT r.name FROM tasks t JOIN repos r ON r.id = t.repo_id WHERE t.id = ?",
        (int(task_id),),
    ).fetchone()
    return str(row["name"]) if row is not None else None


def observe_task(
    task_id: int | None,
    kind: str,
    seconds: float,
    *,
    outcome: str = "",
    at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Keep a duration against the repository of ``task_id``. Never raises."""
    own = conn is None
    try:
        if own:
            from papaya_agent_runtime.state import init_db

            conn = init_db()
        repo = repo_of_task(conn, task_id)
        return observe(repo, kind, seconds, task_id=task_id, outcome=outcome, at=at, conn=conn)
    except (sqlite3.Error, OSError):
        return False
    finally:
        if own and conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def observed(
    conn: sqlite3.Connection, *, task_id: int, kind: str, seconds: float, outcome: str = ""
) -> bool:
    """Is this exact observation already kept? For durations seen more than once (CI)."""
    row = conn.execute(
        "SELECT 1 FROM repo_observations WHERE task_id = ? AND kind = ? AND seconds = ? "
        "AND outcome = ? LIMIT 1",
        (task_id, kind, round(float(seconds), 3), outcome or ""),
    ).fetchone()
    return row is not None


# ── overrides ───────────────────────────────────────────────────────────────


def parse_override(text: str) -> tuple[str, float | None]:
    """``kind=seconds`` to ``(kind, seconds)``; ``0`` or an empty value clears the override."""
    kind, sep, raw = str(text).partition("=")
    kind = kind.strip()
    if not sep or kind not in KINDS:
        raise BudgetError(f"--budget wants <kind>=<seconds> with kind one of {', '.join(KINDS)}")
    raw = raw.strip()
    if not raw:
        return kind, None
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise BudgetError(f"--budget {kind} wants a number of seconds, got {raw!r}") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise BudgetError(f"--budget {kind} wants a non-negative number of seconds, got {raw!r}")
    return kind, (seconds or None)


def set_override(conn: sqlite3.Connection, repo: str, kind: str, seconds: float | None) -> None:
    """Record (or, with ``None``, clear) a person's budget for one repository and kind."""
    if kind not in KINDS:
        raise BudgetError(f"unknown budget kind {kind!r}")
    if seconds is None:
        conn.execute("DELETE FROM repo_budget_overrides WHERE repo = ? AND kind = ?", (repo, kind))
    else:
        conn.execute(
            "INSERT INTO repo_budget_overrides (repo, kind, seconds, set_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(repo, kind) DO UPDATE SET seconds = excluded.seconds, "
            "set_at = excluded.set_at",
            (repo, kind, float(seconds), _stamp(_now())),
        )
    conn.commit()


# ── deriving ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Budget:
    """One repository's budget for one kind, and where it came from."""

    repo: str
    kind: str
    seconds: float
    #: `derived`, `default` or `override`.
    source: str
    #: Observations in the window (after leaving out stalls and kills).
    observations: int
    #: The window's 90th percentile, or ``None`` with no observations.
    p90: float | None
    default: float
    cap: float
    #: What the derivation may not go below (:func:`floor_seconds`); the default when unset.
    floor: float | None = None

    @property
    def derived(self) -> bool:
        return self.source == DERIVED

    @property
    def known(self) -> bool:
        """Is this repository's history behind the number (derived, or at least three)?"""
        return self.observations >= MIN_OBSERVATIONS

    def line(self) -> str:
        """The one line `ppy repo budgets` prints for this kind."""
        p90 = _minutes(self.p90) if self.p90 is not None else "-"
        why = {
            DERIVED: f"derived: p90 x {FACTOR:g}, "
            f"floor {_minutes(self.default if self.floor is None else self.floor)}, "
            f"cap {_minutes(self.cap)}",
            DEFAULT: f"default: fewer than {MIN_OBSERVATIONS} observations"
            if self.observations < MIN_OBSERVATIONS
            else "default",
            OVERRIDE: "override: set with `ppy repo set --budget`",
        }[self.source]
        return (
            f"{self.kind:<15} {self.observations:>3} obs  p90 {p90:>7}  "
            f"budget {_minutes(self.seconds):>7}  {why}"
        )


def _minutes(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(seconds))
    if total < 90:
        return f"{total}s"
    minutes = total // 60
    if minutes < 120:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def window(
    conn: sqlite3.Connection, repo: str, kind: str, *, now: datetime | None = None
) -> list[float]:
    """The durations a derivation reads: newest first, stalls and kills left out."""
    since = (now or _now()) - timedelta(days=WINDOW_DAYS)
    marks = ",".join("?" for _ in EXCLUDED_OUTCOMES)
    rows = conn.execute(
        "SELECT seconds FROM repo_observations WHERE repo = ? AND kind = ? AND at >= ? "
        f"AND outcome NOT IN ({marks}) ORDER BY at DESC, id DESC LIMIT ?",
        (repo, kind, _stamp(since), *EXCLUDED_OUTCOMES, WINDOW_COUNT),
    ).fetchall()
    return [float(row["seconds"]) for row in rows]


def budget(
    repo: str | None,
    kind: str,
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> Budget:
    """The budget for ``kind`` in ``repo``: override, else derived, else the default.

    With no repository, or a state database that cannot be read, it is the default.
    """
    default = default_seconds(kind)
    cap = max(CAPS[kind], default)
    fallback = Budget(str(repo or ""), kind, default, DEFAULT, 0, None, default, cap)
    if not repo:
        return fallback
    own = conn is None
    try:
        if own:
            from papaya_agent_runtime.state import init_db

            conn = init_db()
        values = window(conn, repo, kind, now=now)
        override = conn.execute(
            "SELECT seconds FROM repo_budget_overrides WHERE repo = ? AND kind = ?", (repo, kind)
        ).fetchone()
    except (sqlite3.Error, OSError):
        return fallback
    finally:
        if own and conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
    p90 = percentile(values)
    floor = floor_seconds(kind, default, values)
    if override is not None:
        return Budget(
            repo, kind, float(override["seconds"]), OVERRIDE, len(values), p90, default, cap, floor
        )
    if p90 is None or len(values) < MIN_OBSERVATIONS:
        return Budget(repo, kind, default, DEFAULT, len(values), p90, default, cap, floor)
    seconds = min(cap, max(floor, p90 * FACTOR))
    return Budget(repo, kind, seconds, DERIVED, len(values), p90, default, cap, floor)


def floor_seconds(kind: str, default: float, values: Iterable[float]) -> float:
    """What a derivation may not go below: the default, or for `plan` the observed floor.

    A plan phase is mostly the setup a brief asks for, and a repository whose plans
    take twelve minutes should learn twelve, not be held at a default it never meets
    (#55). Every other kind keeps the default as its floor.
    """
    observed = [float(v) for v in values]
    if kind == PLAN and observed:
        return min(default, min(observed))
    return default


def seconds(repo: str | None, kind: str, *, now: datetime | None = None) -> float:
    """Just the number: what a waiting caller uses in place of the global default."""
    return budget(repo, kind, now=now).seconds


def all_budgets(
    repo: str, *, now: datetime | None = None, conn: sqlite3.Connection | None = None
) -> list[Budget]:
    return [budget(repo, kind, now=now, conn=conn) for kind in KINDS]


@dataclass(frozen=True)
class Memory:
    """One repository's gate peak memory: how much a heavy gate there waits for."""

    repo: str
    observations: int
    #: The window's 90th percentile in megabytes, or ``None`` with no observations.
    p90_mb: float | None

    @property
    def known(self) -> bool:
        return self.observations >= MIN_OBSERVATIONS and self.p90_mb is not None

    def line(self) -> str:
        p90 = megabytes(self.p90_mb) if self.p90_mb is not None else "-"
        why = (
            "peak resident memory of the gate's processes; a heavy gate waits for this much free"
            if self.known
            else f"fewer than {MIN_OBSERVATIONS} observations: a heavy gate does not wait on memory"
        )
        return f"{GATE_MEMORY:<15} {self.observations:>3} obs  p90 {p90:>7}  {why}"


def megabytes(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value / 1024:.1f}G" if value >= 1024 else f"{int(round(value))}M"


def memory(
    repo: str | None, *, now: datetime | None = None, conn: sqlite3.Connection | None = None
) -> Memory:
    """The p90 of this repository's gate peak memory; unknown when the state cannot say."""
    if not repo:
        return Memory("", 0, None)
    own = conn is None
    try:
        if own:
            from papaya_agent_runtime.state import init_db

            conn = init_db()
        values = window(conn, repo, GATE_MEMORY, now=now)
    except (sqlite3.Error, OSError):
        return Memory(str(repo), 0, None)
    finally:
        if own and conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
    return Memory(str(repo), len(values), percentile(values))


def render(repo: str, budgets: list[Budget], memory: Memory | None = None) -> str:
    """What `ppy repo budgets` prints for one repository."""
    lines = [f"{repo}:"]
    lines.extend(f"  {b.line()}" for b in budgets)
    if memory is not None:
        lines.append(f"  {memory.line()}")
    return "\n".join(lines)


def longest_gate(repo: str | None, *, conn: sqlite3.Connection | None = None) -> Budget | None:
    """The longer of the local gate and full suite budgets that history stands behind."""
    known = [
        b for b in (budget(repo, GATE, conn=conn), budget(repo, FULL_SUITE, conn=conn)) if b.derived
    ]
    return max(known, key=lambda b: b.seconds) if known else None


def gate_timing_line(repo: str | None, *, conn: sqlite3.Connection | None = None) -> str | None:
    """How long this repository's gates have taken, for a worker's environment block."""
    parts = []
    for kind, label in ((GATE, "the local gate"), (FULL_SUITE, "the full gate")):
        found = budget(repo, kind, conn=conn)
        if found.known and found.p90 is not None:
            parts.append(
                f"{label} here has taken about {max(1, round(found.p90 / 60))} minutes "
                f"(p90 of its last {found.observations} runs)"
            )
    if not parts:
        return None
    return "; ".join(parts)


__all__ = [
    "BRIEF_TURN",
    "CAPS",
    "CI",
    "DEFAULT",
    "DERIVED",
    "FULL_SUITE",
    "GATE",
    "GATE_MEMORY",
    "KILL",
    "KINDS",
    "MEASURES",
    "OVERRIDE",
    "PLAN",
    "REVIEW_TURN",
    "SILENCE",
    "STALL",
    "TOOL_CAP_SECONDS",
    "WORKER_SESSION",
    "Budget",
    "BudgetError",
    "Memory",
    "all_budgets",
    "budget",
    "default_seconds",
    "floor_seconds",
    "gate_timing_line",
    "longest_gate",
    "megabytes",
    "memory",
    "observe",
    "observe_task",
    "parse_override",
    "percentile",
    "render",
    "seconds",
    "set_override",
    "window",
]
