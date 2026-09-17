"""A heartbeat for the manager: one line of team state on a cadence.

The runtime contract asks the manager to check on every in-flight worker every
5–10 minutes and relay what it sees — even when nothing changed. This command is
the mechanism. Each tick prints one digest line: the in-flight tasks with their
liveness and current phase, the tasks waiting on the manager, the pull requests
the finished work is sitting in (with their CI verdict), the events that arrived
since the previous tick, and the size of the open ledger. A harness monitor wakes
the manager on every line, and the manager relays it. ``--once`` prints a single
tick for scripts and tests.

It exists because on 2026-08-31 a finished worker sat unpushed and a steer went
undelivered while the manager waited, silently, on completion watchers that had
nothing to say — and the user had to ask whether anyone was watching. The pull
request segment exists for the other half of that wait: once work is delivered,
what the manager is actually waiting on is CI, and hand-arming one watcher per
pull request is the same silent failure in a different costume.

The heartbeat is loud while there is something to watch and silent when there is
not. After two consecutive idle ticks it says so once and then prints nothing
until the team has news again — because on 2026-09-04 a finished run left the
watch announcing "no workers in flight" every five minutes for over an hour, and
every one of those ticks woke the manager to relay nothing.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any, TextIO

from papaya_agent_runtime import board, companions, health, owed, supervision
from papaya_agent_runtime.state import init_db, store

DEFAULT_INTERVAL_SECONDS = 300.0

# Statuses that mean "the manager owes this task an action" right now.
NEEDS_ME = owed.OWED_STATUSES

# Statuses whose work has left the worktree, so a pull request may exist for it.
PR_TRACKED = ("worker_done", "delivered")

# Event kinds that are provider chatter, not news the manager relays.
NOISE_PREFIXES = ("worker_item.", "worker_thread.", "worker_turn.", "hook_")

# A heartbeat must not hang on a slow forge; an unanswered query is "unknown".
GH_TIMEOUT_SECONDS = 30.0

# The `gh pr checks --json bucket` values that mean the run is not green.
CI_BAD_BUCKETS = ("fail", "cancel")
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}\Z")


def _clip(text: object, width: int) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= width else one[: width - 1] + "…"


def _latest_phase(conn: sqlite3.Connection, task_id: int) -> str | None:
    # Notes only (`store.PROGRESS_NOTE`): a phaseless stream line is not the latest phase.
    row = conn.execute(
        f"SELECT payload FROM events WHERE task_id = ? AND {store.PROGRESS_NOTE} "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"]).get("phase")
    except (TypeError, ValueError):
        return None


def max_event_id(conn: sqlite3.Connection | None = None) -> int:
    conn = conn or init_db()
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(row["m"])


def _run_gh(args: list[str], cwd: str | None = None) -> tuple[int, str]:
    """Run one read-only ``gh`` query. Returns (exit code, stdout).

    The single seam every pull-request lookup goes through, so tests fake the
    forge by replacing this function and never shell out. A missing, broken, or
    slow ``gh`` is not an error here: it returns a non-zero code and the caller
    reports the pull request state as unknown rather than failing the tick.
    """
    # Plain ``gh`` only: ``gh-axi`` is a different command surface (``--fields``
    # instead of ``--json``, no ``checks --json``), and on 2026-09-04 the first
    # live tick showed every historical branch as ``ci: unknown`` because the
    # resolver had preferred the wrapper.
    tool = companions.companion_bin("gh")
    if tool is None:
        return (127, "")
    try:
        proc = subprocess.run(
            [tool, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return (127, "")
    # `gh pr checks` exits non-zero when checks are failing or pending, so the
    # exit code is advice, not a verdict: the caller judges the payload.
    return (proc.returncode, proc.stdout)


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _ci_verdict(checks: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Summarise `gh pr checks` rows as pass/fail/pending/none, naming what failed."""
    if not checks:
        return ("none", [])
    buckets = {str(c.get("bucket") or "").lower() for c in checks}
    failing = sorted(
        str(c.get("name") or "a check")
        for c in checks
        if str(c.get("bucket") or "").lower() in CI_BAD_BUCKETS
    )
    if failing or buckets & set(CI_BAD_BUCKETS):
        return ("fail", failing)
    if "pending" in buckets:
        return ("pending", [])
    # Everything else — green, or skipped, which blocks nothing — reads as pass.
    return ("pass", [])


def _lookup_pr(branch: str, cwd: str | None, conversation: bool = False) -> dict[str, Any]:
    """The forge's view of one branch: its pull request, mergeability, and CI.

    ``conversation`` also reads what people said on an open pull request since its last
    push (:func:`pr_conversation`): two more queries, which the rounds pay and the
    heartbeat does not.
    """
    unknown = {"known": False, "pr": None, "ci": "unknown", "failing": []}
    if cwd is None:
        return unknown
    code, out = _run_gh(
        [
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,state,mergeable,mergeStateStatus,baseRefName,mergedAt,mergeCommit,url,"
            "reviewDecision,headRefOid,createdAt",
        ],
        cwd=cwd,
    )
    rows = _load_json(out)
    if code != 0 or not isinstance(rows, list):
        return unknown
    if not rows:
        return {"known": True, "pr": None, "ci": "none", "failing": []}
    row = rows[0]
    if not isinstance(row, dict):
        return unknown
    merge_commit = row.get("mergeCommit")
    merge_oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    state = str(row.get("state") or "").upper()
    found = {
        "known": True,
        "pr": row.get("number"),
        "base": row.get("baseRefName"),
        "state": state,
        "mergeable": str(row.get("mergeable") or "UNKNOWN").upper(),
        "merge_state": str(row.get("mergeStateStatus") or "UNKNOWN").upper(),
        "ci": "unknown",
        "failing": [],
        # Keep this an actual bool. Reconciliation below uses ``is True`` so a
        # fake or malformed forge payload such as "true"/1 cannot record a merge.
        "merged": bool(
            state == "MERGED"
            and isinstance(row.get("mergedAt"), str)
            and row.get("mergedAt")
            and isinstance(merge_oid, str)
            and _SHA_RE.fullmatch(merge_oid)
        ),
        "merge_commit": merge_oid if isinstance(merge_oid, str) else None,
        "url": row.get("url") if isinstance(row.get("url"), str) else None,
        # `CHANGES_REQUESTED`, `APPROVED`, `REVIEW_REQUIRED`, or "" with no review.
        "review": str(row.get("reviewDecision") or "").upper(),
        # The commit the pull request is at: what a round's attention is keyed on.
        "head": row.get("headRefOid") if isinstance(row.get("headRefOid"), str) else None,
        "created_at": row.get("createdAt") if isinstance(row.get("createdAt"), str) else None,
    }
    if found["state"] != "OPEN":
        return found  # a merged or closed pull request has nothing left to run
    code, out = _run_gh(
        ["pr", "checks", str(found["pr"]), "--json", "name,bucket,startedAt,completedAt,link"],
        cwd=cwd,
    )
    checks = _load_json(out)
    if isinstance(checks, list):
        found["ci"], found["failing"] = _ci_verdict(checks)
        settled = all(
            isinstance(c, dict) and str(c.get("bucket") or "").lower() != "pending" for c in checks
        )
        if found["ci"] in ("pass", "fail") and settled:
            found["ci_seconds"] = ci_wall_seconds(checks)
        found["pending_checks"] = [
            {"name": str(c.get("name") or "a check"), "started_at": c.get("startedAt")}
            for c in checks
            if isinstance(c, dict) and str(c.get("bucket") or "").lower() == "pending"
        ]
        found["failing_links"] = [
            str(c.get("link"))
            for c in checks
            if isinstance(c, dict)
            and str(c.get("bucket") or "").lower() in CI_BAD_BUCKETS
            and c.get("link")
        ]
    if conversation:
        found.update(pr_conversation(found["pr"], cwd))
    return found


#: Only unresolved review threads, with the first comment of each and when the thread
#: last moved. `gh pr view` has no field for threads, so this is the one GraphQL query.
_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          comments(first: 100) {
            nodes { author { login } body createdAt }
          }
        }
      }
    }
  }
}
"""

_PR_URL_PARTS = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/\d+")


def is_bot(login: object) -> bool:
    """A bot's comment is not a person's: logins ending in `[bot]` are skipped."""
    return str(login or "").endswith("[bot]")


def pr_conversation(pr: object, cwd: str | None) -> dict[str, Any]:
    """What people said on a pull request since its last push, for the rounds.

    Returns ``last_push_at`` (the newest commit's date), ``comments`` (reviews with a
    body and top-level comments, from a person, after that push) and ``threads``
    (unresolved review threads whose newest comment is after that push). Each item has
    a stable ``id``, so the same comment is the same reason next round. Anything
    unreadable is simply absent: a forge that cannot be asked raises nothing.
    """
    code, out = _run_gh(["pr", "view", str(pr), "--json", "reviews,comments,commits,url"], cwd=cwd)
    view = _load_json(out)
    if code != 0 or not isinstance(view, dict):
        return {}
    commits = [c for c in view.get("commits") or [] if isinstance(c, dict)]
    stamps = [str(c.get("committedDate") or "") for c in commits if c.get("committedDate")]
    last_push = max(stamps) if stamps else ""
    after = _parse_stamp(last_push)
    said: list[dict[str, Any]] = []
    for kind, key in (("review", "reviews"), ("comment", "comments")):
        for item in view.get(key) or []:
            if not isinstance(item, dict) or not str(item.get("body") or "").strip():
                continue
            login = (item.get("author") or {}).get("login")
            stamp = item.get("submittedAt") or item.get("createdAt")
            if is_bot(login) or not _after(stamp, after):
                continue
            said.append(
                {
                    "id": str(item.get("id") or f"{kind}:{login}:{stamp}"),
                    "kind": kind,
                    "author": login,
                    "body": str(item.get("body") or "").strip(),
                    "at": stamp,
                }
            )
    threads: list[dict[str, Any]] = []
    match = _PR_URL_PARTS.search(str(view.get("url") or ""))
    if match:
        code, out = _run_gh(
            [
                "api",
                "graphql",
                "-f",
                f"query={_THREADS_QUERY}",
                "-F",
                f"owner={match.group(1)}",
                "-F",
                f"name={match.group(2)}",
                "-F",
                f"number={pr}",
            ],
            cwd=cwd,
        )
        data = _load_json(out)
        nodes = (
            ((((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {})
            .get("reviewThreads", {})
            .get("nodes")
            if code == 0 and isinstance(data, dict)
            else None
        )
        for node in nodes or []:
            if not isinstance(node, dict) or node.get("isResolved"):
                continue
            comments = [
                c for c in (node.get("comments") or {}).get("nodes") or [] if isinstance(c, dict)
            ]
            people = [c for c in comments if not is_bot((c.get("author") or {}).get("login"))]
            if not people or not _after(max(str(c.get("createdAt") or "") for c in people), after):
                continue
            first = people[0]
            threads.append(
                {
                    "id": str(node.get("id") or ""),
                    "path": node.get("path"),
                    "line": node.get("line"),
                    "author": (first.get("author") or {}).get("login"),
                    "body": str(first.get("body") or "").strip(),
                }
            )
    return {"last_push_at": last_push or None, "comments": said, "threads": threads}


def _parse_stamp(stamp: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _after(stamp: object, after: datetime | None) -> bool:
    when = _parse_stamp(stamp)
    if when is None:
        return False
    return after is None or when > after


def ci_wall_seconds(checks: list[dict[str, Any]]) -> float | None:
    """First check started to last check finished, when the forge reports every stamp."""
    starts, ends = [], []
    for check in checks:
        if not isinstance(check, dict):
            return None
        try:
            started = datetime.fromisoformat(
                str(check.get("startedAt") or "").replace("Z", "+00:00")
            )
            completed = datetime.fromisoformat(
                str(check.get("completedAt") or "").replace("Z", "+00:00")
            )
        except ValueError:
            return None
        # GitHub reports a check that never ran with the zero time.
        if started.year < 2000 or completed.year < 2000:
            continue
        starts.append(started)
        ends.append(completed)
    if not starts:
        return None
    seconds = (max(ends) - min(starts)).total_seconds()
    return seconds if seconds >= 0 else None


def _first_existing(*paths: str | None) -> str | None:
    for path in paths:
        if path and os.path.isdir(path):
            return path
    return None


def is_settled(entry: dict[str, Any]) -> bool:
    """Has this pull request stopped being able to change? Merged and closed are terminal."""
    return bool(entry.get("known")) and entry.get("state") in ("MERGED", "CLOSED")


def pr_states(
    conn: sqlite3.Connection,
    *,
    settled: dict[str, dict[str, Any]] | None = None,
    conversation: bool = False,
) -> list[dict[str, Any]]:
    """One entry per delivered/finished task whose branch could still be on the forge.

    Three things keep the forge traffic proportional to live work rather than to
    the length of the history:

    - a task whose merge is on the record (``ppy deliver --merged``) is never
      asked about again — a ``delivered`` task stays delivered forever, so
      without this every task ever shipped would cost a query every tick;
    - a pull request already observed merged or closed in this process is served
      from ``settled`` — terminal states cannot change, so asking again is waste;
    - within a tick, results are cached per branch, so two tasks stacked on one
      branch cost one lookup pair.
    """
    marks = ",".join("?" for _ in PR_TRACKED)
    rows = conn.execute(
        "SELECT t.id AS task_id, t.status, t.branch, t.worktree_path, r.local_path "
        f"FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id WHERE t.status IN ({marks}) "
        "AND t.branch IS NOT NULL AND t.branch != '' "
        "AND (t.merged_sha IS NULL OR t.merged_sha = '') ORDER BY t.id",
        PR_TRACKED,
    ).fetchall()
    cache: dict[str, dict[str, Any]] = dict(settled or {})
    states: list[dict[str, Any]] = []
    for row in rows:
        branch = str(row["branch"])
        if branch not in cache:
            cwd = _first_existing(row["worktree_path"], row["local_path"])
            cache[branch] = (
                _lookup_pr(branch, cwd, conversation=True)
                if conversation
                else _lookup_pr(branch, cwd)
            )
        entry = dict(cache[branch])
        entry["task_id"] = int(row["task_id"])
        entry["branch"] = branch
        entry["status"] = str(row["status"])
        states.append(entry)
    return states


def settled_index(
    previous: dict[str, dict[str, Any]] | None, states: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The by-branch record of pull requests that have reached a terminal state."""
    index = dict(previous or {})
    for entry in states:
        if is_settled(entry):
            index[str(entry["branch"])] = entry
    return index


def _mergeability(entry: dict[str, Any]) -> str:
    if entry.get("mergeable") == "CONFLICTING" or entry.get("merge_state") == "DIRTY":
        return "conflicts"
    if entry.get("merge_state") == "BEHIND":
        return "behind base"
    if entry.get("mergeable") == "MERGEABLE":
        return "mergeable"
    return "mergeability unknown"


def describe_pr(entry: dict[str, Any]) -> str | None:
    """The compact segment for one pull request, or ``None`` when there is nothing to say.

    Only *open* pull requests earn a standing segment. A merged or closed one is
    news exactly once, on the tick it flips, and the change list carries that;
    keeping it on the line forever would bury the live work behind the history.
    """
    if not entry.get("known"):
        # Nothing to relay: an unreadable forge is not news, and one line per
        # historical branch buried the live pull requests on the first live tick.
        return None
    if entry.get("pr") is None:
        return None  # branch has no pull request yet; the task line already says so
    if entry.get("state", "OPEN") != "OPEN":
        return None
    head = f"PR #{entry['pr']}"
    if entry.get("base"):
        head += f" -> {entry['base']}"
    ci = entry.get("ci", "unknown")
    if ci == "fail" and entry.get("failing"):
        ci = f"fail ({', '.join(entry['failing'])})"
    elif ci == "none":
        ci = "no checks"
    return f"{head}: ci {ci}, {_mergeability(entry)}"


def pr_index(states: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keyed by pull request when there is one, else by branch, so ticks compare like with like."""
    index: dict[str, dict[str, Any]] = {}
    for entry in states:
        if not entry.get("known"):
            continue
        key = f"PR #{entry['pr']}" if entry.get("pr") else f"branch {entry['branch']}"
        index[key] = entry
    return index


def pr_changes(
    previous: dict[str, dict[str, Any]] | None, current: dict[str, dict[str, Any]]
) -> list[str]:
    """What flipped on the forge since the last tick, in words the manager can relay.

    ``previous`` is ``None`` on the very first tick — there is no "since" yet, so
    nothing has changed. An empty mapping is different: the last tick genuinely
    saw no pull requests, and one showing up now is news.
    """
    if previous is None:
        return []
    changes: list[str] = []
    for key, now in current.items():
        was = previous.get(key)
        if was is None:
            changes.append(f"{key} appeared")
            continue
        if was.get("state") != now.get("state") and now.get("state"):
            old_state = str(was.get("state", "unknown")).lower()
            changes.append(f"{key} {old_state} -> {str(now['state']).lower()}")
        if was.get("ci") != now.get("ci"):
            changes.append(f"{key} ci {was.get('ci', 'unknown')} -> {now.get('ci', 'unknown')}")
        if _mergeability(was) != _mergeability(now):
            changes.append(f"{key} {_mergeability(was)} -> {_mergeability(now)}")
    return changes


def tick(
    conn: sqlite3.Connection | None = None,
    *,
    since_event_id: int = 0,
    now: datetime | None = None,
    previous_prs: dict[str, dict[str, Any]] | None = None,
    settled_prs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One snapshot of team state, plus the events that arrived since ``since_event_id``."""
    conn = conn or init_db()
    now = now or datetime.now(UTC)
    in_flight = health.check(conn, now=now)
    for entry in in_flight:
        entry["phase"] = _latest_phase(conn, entry["task_id"])
    prs = pr_states(conn, settled=settled_prs)
    recorded_merges: list[dict[str, Any]] = []
    for entry in prs:
        if entry.get("state") != "MERGED" or entry.get("merged") is not True:
            continue
        merged_sha = entry.get("merge_commit")
        if not isinstance(merged_sha, str) or not _SHA_RE.fullmatch(merged_sha):
            continue
        from papaya_agent_runtime.delivery import DeliveryError, record_merged

        try:
            result = record_merged(
                int(entry["task_id"]),
                merged_sha,
                note=f"ppy watch observed pull request #{entry.get('pr')} merged",
            )
        except DeliveryError:
            continue
        recorded_merges.append(
            {
                "task_id": result.task_id,
                "pr": entry.get("pr"),
                "merge_commit": result.head_sha,
                "compose": result.compose,
                "note": result.note,
            }
        )
    # The same list every surface reads (`owed`): failures included, with why and what next.
    needs_me = [{"id": item.task_id, **item.public()} for item in owed.collect(conn, now=now)]
    # The check-ins serve's rounds would queue, for every live worker (`supervision`).
    checkins = [
        {"task_id": c.worker_task_id, "reason": c.reason, "ticket_task_id": c.ticket_task_id}
        for c in supervision.worker_checkins(now=now)
    ]
    new_events: dict[str, int] = {}
    last_id = since_event_id
    for row in conn.execute(
        "SELECT id, kind FROM events WHERE id > ? ORDER BY id", (since_event_id,)
    ).fetchall():
        last_id = int(row["id"])
        kind = str(row["kind"])
        if kind.startswith(NOISE_PREFIXES):
            continue
        new_events[kind] = new_events.get(kind, 0) + 1
    snapshot = {
        "at": now.isoformat(timespec="seconds"),
        "in_flight": in_flight,
        "needs_me": needs_me,
        "checkins": checkins,
        "new_events": new_events,
        "last_event_id": last_id,
        "open_todos": len(board.open_todos(conn)),
        "prs": prs,
        "recorded_merges": recorded_merges,
        "usage_advisories": health.usage_advisories(conn),
        "pr_changes": pr_changes(previous_prs, pr_index(prs)),
    }
    snapshot["idle"] = is_idle(snapshot)
    return snapshot


def is_idle(snapshot: dict[str, Any]) -> bool:
    """Is there nothing for the manager to watch right now?

    Idle is the conjunction of every reason a tick could matter: no worker in
    flight, nothing waiting on the manager, no pull request whose checks are
    still running or already failing, and nothing new — no event, no pull request
    that flipped — since the last tick. An open ledger is *not* a reason to keep
    ticking: a todo waits on the manager's next move, not on the team.

    A pull request whose state is ``unknown`` (no ``gh``, or a query that failed)
    does not hold the watch open; a machine that cannot see the forge would
    otherwise never fall quiet.
    """
    if snapshot["in_flight"] or snapshot["needs_me"] or snapshot.get("checkins"):
        return False
    if snapshot["new_events"] or snapshot["pr_changes"] or snapshot.get("recorded_merges"):
        return False
    return not any(entry.get("ci") in ("pending", "fail") for entry in snapshot["prs"])


def _when(snapshot: dict[str, Any]) -> str:
    return snapshot["at"][11:16] + " UTC"


def render(snapshot: dict[str, Any]) -> str:
    """The one line a harness monitor relays: what is running, what is owed, what is new."""
    when = _when(snapshot)
    if snapshot["in_flight"]:
        parts = []
        for e in snapshot["in_flight"]:
            piece = f"t{e['task_id']} {e.get('phase') or e['status']} {e['verdict']}"
            if e["verdict"] == "quiet" and e.get("silent_seconds"):
                piece += f" ({health.humanize(e['silent_seconds'])} silent)"
            parts.append(piece)
        in_flight = f"in flight {len(parts)}: " + "; ".join(parts)
    else:
        in_flight = "no workers in flight"
    needs = (
        "; ".join(
            f"t{r['id']} {r['status']}"
            + (f" ({_clip(r['reason'], 90)})" if r.get("reason") else "")
            for r in snapshot["needs_me"]
        )
        or "none"
    )
    new = ", ".join(f"{k}×{v}" for k, v in sorted(snapshot["new_events"].items())) or "none"
    line = f"TEAM {when} — {in_flight} | needs me: {needs}"
    if snapshot.get("checkins"):
        line += " | check in: " + "; ".join(
            f"t{c['task_id']} ({_clip(c['reason'], 90)})" for c in snapshot["checkins"]
        )
    segments = [s for s in (describe_pr(e) for e in snapshot.get("prs", [])) if s]
    if segments:
        line += " | prs: " + "; ".join(segments)
    line += f" | new since last tick: {new}"
    if snapshot.get("pr_changes"):
        line += " | changed: " + "; ".join(snapshot["pr_changes"])
    if snapshot.get("recorded_merges"):
        landed = "; ".join(
            f"task {m['task_id']} PR #{m['pr']} recorded merged at {m['merge_commit'][:8]}"
            for m in snapshot["recorded_merges"]
        )
        line += " | merged: " + landed
    if snapshot.get("usage_advisories"):
        line += " | " + "; ".join(
            health.describe_usage_advisory(item) for item in snapshot["usage_advisories"]
        )
    return line + f" | open todos: {snapshot['open_todos']}"


def run(
    interval: float = DEFAULT_INTERVAL_SECONDS,
    *,
    once: bool = False,
    as_json: bool = False,
    exit_when_idle: bool = False,
    out: TextIO | None = None,
    sleep=time.sleep,
    clock=None,
) -> int:
    """Print a tick now and then every ``interval`` seconds until interrupted.

    The watch keeps ticking, but it stops talking. After two consecutive idle
    ticks it says so once and then prints nothing at all until something changes
    — a dispatch, a resume, an event, a check flipping, a worker going quiet —
    at which point the normal line and the five-minute cadence resume on their
    own. The process stays alive through all of it, deliberately: the manager
    runs this under a persistent monitor, and a silent process keeps that monitor
    armed with nothing to re-arm. ``--exit-when-idle`` is the opt-out for scripts
    that want a process that ends.
    """
    out = out or sys.stdout  # resolved per call so harness capture sees the ticks
    conn = init_db()
    if not once:
        # The Stop hook asks whether a heartbeat is armed before a turn may end with
        # work in flight; this is how it knows.
        owed.mark_watch_running()
        try:
            return _loop(conn, interval, as_json, exit_when_idle, out, sleep, clock)
        finally:
            owed.clear_watch_mark()
    return _loop(conn, interval, as_json, exit_when_idle, out, sleep, clock, once=True)


def _loop(conn, interval, as_json, exit_when_idle, out, sleep, clock, *, once=False) -> int:
    # The first tick reports the current state, not the whole event history.
    since = max_event_id(conn)
    previous: dict[str, dict[str, Any]] | None = None
    settled: dict[str, dict[str, Any]] = {}
    idle_ticks = 0

    def emit(snapshot: dict[str, Any], text: str) -> None:
        print(json.dumps(snapshot, default=str) if as_json else text, file=out)
        out.flush()

    while True:
        snapshot = tick(
            conn,
            since_event_id=since,
            previous_prs=previous,
            settled_prs=settled,
            now=clock() if clock else None,
        )
        since = snapshot["last_event_id"]
        previous = pr_index(snapshot["prs"])
        settled = settled_index(settled, snapshot["prs"])
        if once:  # a single tick is always spoken, idle or not
            emit(snapshot, render(snapshot))
            return 0
        idle_ticks = idle_ticks + 1 if snapshot["idle"] else 0
        if idle_ticks >= 2 and exit_when_idle:
            emit(snapshot, f"TEAM {_when(snapshot)} — idle; watch exiting (--exit-when-idle)")
            return 0
        if idle_ticks < 2:
            emit(snapshot, render(snapshot))
        elif idle_ticks == 2:
            emit(snapshot, f"TEAM {_when(snapshot)} — idle; watch quiet until something changes")
        # Past that, an idle tick says nothing at all: silence is the report.
        sleep(interval)
