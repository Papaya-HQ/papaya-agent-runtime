"""The runtime's own deficiencies, and the GitHub issues it opens about them.

Every structural problem in this runtime so far was found by a person reading
transcripts after the fact: PAP-213's turns backgrounding a gate, the PAP-219 stall
while the worker was mid-gate, the stored default profile, the tool denials. The
runtime saw each of them first and said nothing. This module is where it says so.

A **deficiency** is something wrong with the runtime itself: the runtime, its turns,
the client contract, the supervisor, the prompts. Never the user's code, and never a
worker's mistake on a ticket. Each one is a row in the ``deficiencies`` ledger keyed
on a stable fingerprint (its kind and its normalised detail), with a count and the
evidence of every occurrence. The signals are collected where they already happen
(:func:`record` is called from `serve`, the rounds, the sweep, the supervisor and
the worker runner) and :func:`record` never raises: a report about the runtime must
never be the thing that breaks it.

`ppy serve` turns the ledger into issues on the runtime's own repository
(:class:`Reporter`): one issue per fingerprint, opened when a deficiency is first
recorded, or once it reaches its kind's threshold within one repository or ticket
for the noisy kinds (worker denials and repeated steers, two). A recurrence is one
comment with the new evidence; a closed issue that recurs is reopened with that
comment rather than duplicated. At most ``self_report.max_per_day`` new issues open
a day and the rest wait in the ledger. ``self_report.enabled = false`` keeps the
ledger and opens nothing.

Nothing private leaves the machine. An issue body is built only from the evidence
fields named in :data:`EVIDENCE_FIELDS` — ticket keys and repository names, never
titles, descriptions, comments or people — and every string in it goes through
:func:`redact`, which drops diff hunks, turns home-directory paths into ``~``,
removes tokens and email addresses, and blanks any string the caller names as
private (a ticket's title, a display name).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import threading
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime import tool_learning

log = logging.getLogger("papaya_agent_runtime.deficiencies")

#: The label every self-reported issue carries, beside its kind.
LABEL = "self-reported"

# ── kinds ───────────────────────────────────────────────────────────────────

#: A turn ended with a `RUNTIME: <one line>` line.
TURN_REPORT = "turn-report"
#: A readiness finding the runtime owns was still there after its start-up remedies.
READINESS_UNREMEDIED = "readiness-unremedied"
#: The client handed a ticket back as stalled while its worker session was live.
STALL_WHILE_LIVE = "stall-while-live"
#: A manager turn ended twice without doing its job, and the ticket was handed back.
MISSED_TURN = "missed-turn"
#: A worker's gate was backgrounded past the tool cap with no `ppy gate run` on record.
GATE_PAST_TOOL_CAP = "gate-past-tool-cap"
#: A worker was denied a tool outside the safe family.
WORKER_DENIAL = "worker-denial"
#: An exception escaped `serve`, a turn, a round, the sweep or the supervisor.
UNHANDLED_EXCEPTION = "unhandled-exception"
#: A check-in steered a worker for the same reason again.
REPEATED_STEER = "repeated-steer"
#: CI went red on a pull request the runtime delivered to its own repository.
RUNTIME_CI_RED = "runtime-ci-red"

#: Ledger statuses: below its threshold; ready for an issue; an issue exists.
WATCHING = "watching"
PENDING = "pending"
REPORTED = "reported"


@dataclass(frozen=True)
class Kind:
    """How one kind of deficiency is titled, explained, and when it is worth an issue."""

    title: str
    #: What happened, in one paragraph; ``{detail}`` is the redacted detail.
    happened: str
    #: What the runtime did instead.
    instead: str
    #: A proposed remedy, when the signal implies one.
    remedy: str = ""
    #: Occurrences within one scope (a repository, a ticket) before an issue opens.
    threshold: int = 1


KINDS: dict[str, Kind] = {
    TURN_REPORT: Kind(
        title="A turn said the runtime got in its way",
        happened=(
            "A manager turn ended with a `RUNTIME:` line, which every turn prompt reserves "
            "for the runtime (not the repository) getting in the way of its job: a missing "
            "tool, a fact the turn could not obtain, or a contract that was not true. "
            "It said: {detail}"
        ),
        instead=(
            "The turn went as far as it could and the ticket carried on under the runner's "
            "usual rules: a turn that did not do its job is run once more, then handed back."
        ),
        remedy="Give turns what this one said it was missing, or correct the contract it read.",
    ),
    READINESS_UNREMEDIED: Kind(
        title="A readiness finding the runtime owns was not remedied",
        happened=(
            "`ppy serve` started with a readiness finding that names the runtime as its owner "
            "and blocks work ({detail}), and it was still there after the start-up remedies "
            "(first-run setup) had run. Nothing the runtime does closes it by itself."
        ),
        instead=(
            "Kept listening; every ticket is declined while a blocking finding stands, so a "
            "peer that can do the work may take it."
        ),
        remedy=(
            "Add a remedy for this finding to `serve`'s start-up, or make it a person's item "
            "if no code can close it."
        ),
    ),
    STALL_WHILE_LIVE: Kind(
        title="A ticket was handed back as stalled while its worker was running",
        happened=(
            "The client's stall check ended the hold on a ticket as stalled while the "
            "ticket's worker session was live: the activity the check reads was not touched "
            "while the work went on ({detail})."
        ),
        instead=(
            "Recorded the ticket as `stalled`, set the work item to `todo` and said so on it; "
            "the worker's branch is kept and the ticket can be offered again."
        ),
        remedy=(
            "Count a live worker session (its tool progress) as the job's activity, so a "
            "working worker never reads as quiet."
        ),
    ),
    MISSED_TURN: Kind(
        title="A manager turn ended without doing its job",
        happened=(
            "A manager turn ended without {detail}, twice in a row on the record, so the runner "
            "handed the ticket back. That is the PAP-213 shape: a turn that runs out before its "
            "obligation, not a decision that the work cannot be done."
        ),
        instead=(
            "Declined the ticket and set the work item to `todo`, keeping any worker branch; "
            "the rounds offer a ticket declined for a missed turn again."
        ),
        remedy=(
            "Read the transcripts in the evidence for what the turn was doing when it ended; "
            "a gate outlasting the turn, or a tool it could not use, is the usual cause."
        ),
    ),
    GATE_PAST_TOOL_CAP: Kind(
        title="A gate ran past the tool cap without `ppy gate run`",
        happened=(
            "A worker's session ended with a backgrounded command as its last tool call and no "
            "`ppy gate run` on record for its task ({detail}): the gate outlasted the "
            "harness's ten-minute tool cap and died with the session."
        ),
        instead=(
            "Recorded the worker as `worker_stopped`; the runner sends it back to run its gate "
            "through `ppy gate run`."
        ),
        remedy=(
            "Make `ppy gate run` reachable from the worker (its tool profile and its brief), "
            "and name it wherever a gate may take longer than ten minutes."
        ),
    ),
    WORKER_DENIAL: Kind(
        title="Workers were denied a tool outside the safe family",
        happened=(
            "Workers in one repository were refused {detail} more than once. It is outside "
            "the safe family, so the runtime does not learn it, and every dispatch there "
            "meets the same refusal."
        ),
        instead="Learned nothing; the workers carried on without the tool.",
        remedy=(
            "Decide whether the pattern belongs in the worker profile, or whether the briefs "
            "for that repository should route around it."
        ),
        threshold=2,
    ),
    UNHANDLED_EXCEPTION: Kind(
        title="An unhandled exception",
        happened="An exception escaped {detail} and was caught at the top.",
        instead=(
            "Logged it and recorded the traceback in the evidence; the loop it escaped from "
            "carried on where it could."
        ),
    ),
    REPEATED_STEER: Kind(
        title="A check-in steered twice for the same reason",
        happened=(
            "The rounds' check-in steered one ticket's worker for the same reason ({detail}) "
            "more than once: the first steer did not change what the check-in saw."
        ),
        instead="Delivered each steer; the worker kept running.",
        remedy=(
            "Either the worker cannot act on that steer, or the signal that starts the "
            "check-in stays true after it has; look at what the check-in reads for it."
        ),
        threshold=2,
    ),
    RUNTIME_CI_RED: Kind(
        title="CI went red on a pull request the runtime delivered to itself",
        happened=(
            "A pull request the runtime delivered to its own repository has a failing CI gate "
            "({detail}), after the worker's gate and the review's re-check passed locally."
        ),
        instead=(
            "Recorded `pr_attention` on the worker and moved the ticket back to `dispatched`, "
            "so the review turn steers the worker with the failure."
        ),
        remedy="Close the gap between this repository's local gate and what CI runs.",
    ),
}

#: The evidence fields an issue may carry. Anything else a caller passes is dropped.
EVIDENCE_FIELDS = (
    "phase",
    "turn",
    "where",
    "code",
    "trigger",
    "pattern",
    "command",
    "repo",
    "ticket",
    "task_id",
    "worker_task_id",
    "run_id",
    "event_id",
    "pr",
    "transcript",
    "error",
)

#: How many occurrences a ledger row keeps in full.
EVIDENCE_KEPT = 50
#: The longest error text an occurrence keeps, from its end (where a traceback says why).
ERROR_CHARS = 3000

# ── privacy ─────────────────────────────────────────────────────────────────

_HOME_PATH = re.compile(r"(?:/Users|/home)/[^/\s:'\"`]+")
_TOKENS = (
    re.compile(r"\b(?:gh[pousr]_|github_pat_|pagc_|papaya_|glpat-|xox[abprs]-)[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bsk-(?:ant-|live-|test-|proj-)?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|token|api[_-]?key|secret|password|passwd|client_token)"
    r"(\s*[:=]\s*)(?:bearer\s+)?[\"']?[^\s\"',;]{6,}"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_.\-]{8,}")
#: A long run of letters and digits mixed together: a key, not a SHA or a word.
_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9_\-]*[0-9])(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b")
_HEX = re.compile(r"^[0-9a-f]+$")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_HUNK_START = re.compile(r"^(?:diff --git |@@ |--- a/|\+\+\+ b/|index [0-9a-f]+\.\.[0-9a-f]+)")
REDACTED = "[redacted]"


def _drop_diffs(text: str) -> str:
    """Remove every diff hunk: a header and the `+`, `-` and context lines after it."""
    kept: list[str] = []
    in_hunk = False
    for line in text.splitlines():
        if _HUNK_START.match(line):
            in_hunk = True
            continue
        if in_hunk and (line[:1] in ("+", "-", " ", "\\") or line == ""):
            continue
        in_hunk = False
        kept.append(line)
    if len(kept) < len(text.splitlines()):
        kept.append("[diff removed]")
    return "\n".join(kept)


def _opaque(match: re.Match) -> str:
    word = match.group(0)
    return word if _HEX.match(word.lower()) else REDACTED


def redact(text: object, scrub: Iterable[str] = ()) -> str:
    """``text`` with nothing private left in it.

    Diff hunks are dropped; paths under a home directory start at ``~`` (the user
    name in the path is often a person's); tokens, secret assignments, opaque keys
    and email addresses become ``[redacted]``; and each string in ``scrub`` — a
    ticket's title, its description, a display name — is blanked wherever it
    appears, longest first.
    """
    out = _drop_diffs(str(text or ""))
    for secret in sorted({str(s).strip() for s in scrub if str(s or "").strip()}, key=len)[::-1]:
        if len(secret) >= 3:
            out = out.replace(secret, REDACTED)
    home = str(Path.home())
    if home and home not in ("/", ""):
        out = out.replace(home, "~")
    out = _HOME_PATH.sub("~", out)
    for pattern in _TOKENS:
        out = pattern.sub(REDACTED, out)
    out = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _BEARER.sub(f"Bearer {REDACTED}", out)
    out = _OPAQUE.sub(_opaque, out)
    return _EMAIL.sub(REDACTED, out)


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def normalise(detail: str) -> str:
    """The part of a detail that names the problem, not the occurrence.

    Numbers, hexadecimal ids and quoted strings vary between occurrences of the same
    deficiency ("task 9", "task 12"), so they are replaced before fingerprinting.
    """
    text = _one_line(detail).lower()
    text = re.sub(r"\b[0-9a-f]{7,}\b", "<id>", text)
    text = re.sub(r"\d+", "<n>", text)
    text = re.sub(r"~[^\s]*", "<path>", text)
    return text


def fingerprint(kind: str, detail: str) -> str:
    """A stable id for one deficiency: its kind and its normalised detail."""
    return hashlib.sha256(f"{kind}\n{normalise(detail)}".encode()).hexdigest()[:16]


# ── the ledger ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Deficiency:
    """One ledger row."""

    fingerprint: str
    kind: str
    title: str
    detail: str
    first_seen: str
    last_seen: str
    count: int
    evidence: list[dict[str, Any]]
    issue_url: str | None
    status: str
    opened_at: str | None
    reported_count: int

    @classmethod
    def from_row(cls, row: Any) -> Deficiency:
        try:
            evidence = json.loads(row["evidence"] or "[]")
        except (TypeError, ValueError):
            evidence = []
        return cls(
            fingerprint=str(row["fingerprint"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            detail=str(row["detail"] or ""),
            first_seen=str(row["first_seen"]),
            last_seen=str(row["last_seen"]),
            count=int(row["count"]),
            evidence=evidence if isinstance(evidence, list) else [],
            issue_url=row["issue_url"] or None,
            status=str(row["status"]),
            opened_at=row["opened_at"] or None,
            reported_count=int(row["reported_count"] or 0),
        )


_listeners: list[Callable[[], None]] = []
_listeners_lock = threading.Lock()


def add_listener(listener: Callable[[], None]) -> None:
    """Call ``listener`` after every record in this process (`serve`'s reporter)."""
    with _listeners_lock:
        _listeners.append(listener)


def remove_listener(listener: Callable[[], None]) -> None:
    with _listeners_lock:
        if listener in _listeners:
            _listeners.remove(listener)


def notify() -> None:
    """Tell the listeners the ledger may have something new. Never raises."""
    with _listeners_lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener()
        except Exception as exc:  # noqa: BLE001 - a listener must never break a record
            log.warning("[deficiencies] A listener failed: %s", exc)


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(clock: Callable[[], datetime] | None) -> str:
    return (clock or _now)().isoformat(timespec="seconds")


def _clean_evidence(evidence: Mapping[str, Any] | None, scrub: Iterable[str]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in EVIDENCE_FIELDS:
        value = (evidence or {}).get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            value = redact(value, scrub)
            limit = ERROR_CHARS if key == "error" else 300
            value = value[-limit:] if key == "error" else _one_line(value)[:limit]
        clean[key] = value
    return clean


def record(
    kind: str,
    detail: str,
    *,
    evidence: Mapping[str, Any] | None = None,
    scope: str | None = None,
    scrub: Iterable[str] = (),
    clock: Callable[[], datetime] | None = None,
) -> Deficiency | None:
    """Record one occurrence of a deficiency. Returns the row, or ``None``. Never raises.

    ``scope`` is what a noisy kind's threshold counts within ("repo:<name>",
    "ticket:<key>"); ``scrub`` names strings that must not leave the machine even
    if they turn up in the detail or the error text.
    """
    try:
        found = _record(kind, detail, evidence, scope, list(scrub), clock)
    except Exception as exc:  # noqa: BLE001 - reporting the runtime must never break it
        log.warning("[deficiencies] Could not record a %s deficiency: %s", kind, exc)
        return None
    notify()
    return found


def _record(
    kind: str,
    detail: str,
    evidence: Mapping[str, Any] | None,
    scope: str | None,
    scrub: list[str],
    clock: Callable[[], datetime] | None,
) -> Deficiency:
    from papaya_agent_runtime.state import init_db

    spec = KINDS[kind]
    clean = _one_line(redact(detail, scrub))[:300] or "(no detail)"
    key = fingerprint(kind, clean)
    at = _stamp(clock)
    entry = {"at": at, **_clean_evidence(evidence, scrub)}
    if scope:
        entry["scope"] = redact(scope, scrub)
    conn = init_db()
    try:
        row = conn.execute("SELECT * FROM deficiencies WHERE fingerprint = ?", (key,)).fetchone()
        if row is None:
            entry["n"] = 1
            entries = [entry]
            count = 1
            status = WATCHING
            title = f"{spec.title}: {clean}"
            if len(title) > 120:
                title = title[:117].rstrip() + "..."
            conn.execute(
                "INSERT INTO deficiencies (fingerprint, kind, title, detail, first_seen, "
                "last_seen, count, evidence, status, reported_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (key, kind, title, clean, at, at, count, "[]", status),
            )
        else:
            current = Deficiency.from_row(row)
            count = current.count + 1
            entry["n"] = count
            entries = (current.evidence + [entry])[-EVIDENCE_KEPT:]
            status = current.status
        if status == WATCHING:
            within = [e for e in entries if e.get("scope") == entry.get("scope")]
            if len(within) >= spec.threshold:
                status = PENDING
        conn.execute(
            "UPDATE deficiencies SET last_seen = ?, count = ?, evidence = ?, status = ? "
            "WHERE fingerprint = ?",
            (at, count, json.dumps(entries), status, key),
        )
        conn.commit()
        found = conn.execute("SELECT * FROM deficiencies WHERE fingerprint = ?", (key,)).fetchone()
        return Deficiency.from_row(found)
    finally:
        conn.close()


def ledger(*, include_all: bool = False) -> list[Deficiency]:
    """The ledger, newest first. Below-threshold rows only with ``include_all``."""
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    try:
        rows = conn.execute("SELECT * FROM deficiencies ORDER BY last_seen DESC").fetchall()
    finally:
        conn.close()
    found = [Deficiency.from_row(row) for row in rows]
    return found if include_all else [d for d in found if d.status != WATCHING]


def summary() -> dict[str, int]:
    """Counts for `ppy doctor` and `serve`'s start line. Never raises, never creates a ledger."""
    from papaya_agent_runtime.paths import db_path

    try:
        rows = ledger(include_all=True) if db_path().exists() else []
    except Exception:  # noqa: BLE001 - diagnostics must not crash
        return {"recorded": 0, "open_issues": 0, "waiting": 0}
    return {
        "recorded": len(rows),
        "open_issues": sum(1 for d in rows if d.status == REPORTED),
        "waiting": sum(1 for d in rows if d.status == PENDING),
    }


# ── the runtime's own repository ────────────────────────────────────────────

_GITHUB = re.compile(r"github\.com[:/]+([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$")


def github_slug(url: str | None) -> str | None:
    """``owner/name`` for a GitHub URL (https, ssh or scp form), else ``None``."""
    match = _GITHUB.search(str(url or "").strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def origin_url() -> str | None:
    """The `origin` of the running checkout, or ``None`` when there is none to read."""
    from papaya_agent_runtime.manager.launch import repo_root

    try:
        done = subprocess.run(
            ["git", "-C", str(repo_root()), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    repo: str = ""
    max_per_day: int = 5


def settings() -> Settings:
    """``self_report.*`` from the config, or the defaults when there is none."""
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        cfg = load_config().self_report
    except (ConfigError, OSError):
        return Settings()
    return Settings(enabled=cfg.enabled, repo=cfg.repo, max_per_day=cfg.max_per_day)


def runtime_repo(
    config: Settings | None = None, origin: Callable[[], str | None] | None = None
) -> str | None:
    """The runtime's own GitHub repository as ``owner/name``, or ``None``.

    ``self_report.repo`` wins; otherwise the origin of the running checkout. A
    configured value that is not a GitHub URL is taken as ``owner/name`` as written.
    """
    config = config or settings()
    if config.repo:
        return github_slug(config.repo) or config.repo.strip().strip("/")
    return github_slug((origin or origin_url)())


# ── GitHub, through `gh` ────────────────────────────────────────────────────


def run_gh(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    """Run `gh` and return ``(exit code, stdout, stderr)``; a missing `gh` is 127."""
    try:
        done = subprocess.run(
            ["gh", *args], input=stdin, capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        return 127, "", "gh is not installed"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout, done.stderr


class GhForge:
    """Issues on GitHub through the `gh` the runtime is signed in with.

    ``run`` is the seam: a callable taking `gh`'s arguments and its stdin and
    answering ``(exit code, stdout, stderr)``, which is what a test's fake `gh` is.
    """

    def __init__(self, run: Callable[[list[str], str | None], tuple[int, str, str]] | None = None):
        self._run = run
        self._labels: set[tuple[str, str]] = set()

    def _gh(self, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        return (self._run or run_gh)(args, stdin)

    def _ensure_label(self, repo: str, label: str) -> None:
        if (repo, label) in self._labels:
            return
        description = (
            "Opened by the runtime about itself"
            if label == LABEL
            else "Self-reported deficiency kind"
        )
        self._gh(
            ["label", "create", label, "--repo", repo, "--force", "--color", "d4c5f9"]
            + ["--description", description]
        )
        self._labels.add((repo, label))

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> str | None:
        for label in labels:
            self._ensure_label(repo, label)
        args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", "-"]
        for label in labels:
            args += ["--label", label]
        code, out, err = self._gh(args, body)
        if code != 0:
            log.warning("[deficiencies] gh could not open an issue in %s: %s", repo, err.strip())
            return None
        urls = [line.strip() for line in out.splitlines() if line.strip().startswith("http")]
        return urls[-1] if urls else None

    def state(self, url: str) -> str | None:
        code, out, _err = self._gh(["issue", "view", url, "--json", "state"])
        if code != 0:
            return None
        try:
            return str(json.loads(out).get("state") or "").upper() or None
        except (ValueError, AttributeError):
            return None

    def comment(self, url: str, body: str) -> bool:
        code, _out, err = self._gh(["issue", "comment", url, "--body-file", "-"], body)
        if code != 0:
            log.warning("[deficiencies] gh could not comment on %s: %s", url, err.strip())
        return code == 0

    def reopen(self, url: str, body: str) -> bool:
        code, _out, err = self._gh(["issue", "reopen", url, "--comment", body])
        if code != 0:
            log.warning("[deficiencies] gh could not reopen %s: %s", url, err.strip())
        return code == 0


# ── what an issue says ──────────────────────────────────────────────────────

_EVIDENCE_LABELS = {
    "phase": "phase",
    "turn": "turn",
    "where": "in",
    "code": "finding",
    "trigger": "reason",
    "pattern": "pattern",
    "command": "command",
    "repo": "repo",
    "ticket": "ticket",
    "task_id": "task",
    "worker_task_id": "worker task",
    "run_id": "run",
    "event_id": "event",
    "pr": "pull request",
    "transcript": "transcript",
}


def _evidence_lines(entries: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        parts = [
            f"{label} `{entry[key]}`"
            for key, label in _EVIDENCE_LABELS.items()
            if entry.get(key) not in (None, "")
        ]
        head = f"- {entry.get('at', '?')}"
        lines.append(head + (" — " + ", ".join(parts) if parts else ""))
        if entry.get("error"):
            lines += ["", "  ```", *[f"  {line}" for line in str(entry["error"]).splitlines()]]
            lines += ["  ```"]
    return lines


def _body_text(value: str, scrub: Iterable[str] = ()) -> str:
    # Rendering redacts again: a row written by an older build is held to today's rules.
    return redact(value, scrub)


def issue_body(deficiency: Deficiency) -> str:
    """The first report of a deficiency: what, evidence, what instead, a remedy."""
    spec = KINDS.get(deficiency.kind) or KINDS[UNHANDLED_EXCEPTION]
    lines = [
        "## What happened",
        "",
        spec.happened.format(detail=deficiency.detail),
        "",
        "## Evidence",
        "",
        *_evidence_lines(deficiency.evidence),
        "",
        "## What the runtime did instead",
        "",
        spec.instead,
    ]
    if spec.remedy:
        lines += ["", "## Proposed remedy", "", spec.remedy]
    lines += [
        "",
        "---",
        f"Self-reported by `ppy serve`: kind `{deficiency.kind}`, fingerprint "
        f"`{deficiency.fingerprint}`, seen {deficiency.count} time(s) since "
        f"{deficiency.first_seen}. `ppy deficiency list` shows the ledger.",
    ]
    return _body_text("\n".join(lines))


def comment_body(deficiency: Deficiency, entries: list[dict[str, Any]]) -> str:
    """One comment for a recurrence: the new evidence and the count."""
    lines = [
        f"Seen again: {deficiency.count} time(s) in all, last at {deficiency.last_seen}.",
        "",
        *_evidence_lines(entries),
    ]
    return _body_text("\n".join(lines))


# ── opening issues ──────────────────────────────────────────────────────────


class Reporter:
    """Turns the ledger into issues on the runtime's own repository. `serve` runs one.

    Every collaborator with an outside world is a seam: ``forge`` (GitHub, a
    :class:`GhForge` by default), ``clock`` (an aware ``datetime``; the daily cap is
    counted on its UTC date), ``config`` (a :class:`Settings`, read from
    ``self_report.*`` when absent) and ``origin`` (the checkout's origin URL).
    """

    def __init__(
        self,
        *,
        forge: Any = None,
        clock: Callable[[], datetime] | None = None,
        config: Settings | Callable[[], Settings] | None = None,
        origin: Callable[[], str | None] | None = None,
    ) -> None:
        self._forge = forge or GhForge()
        self._clock = clock or _now
        self._config = config
        self._origin = origin
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._dirty = False
        self._thread: threading.Thread | None = None
        self._warned = False

    def _settings(self) -> Settings:
        if isinstance(self._config, Settings):
            return self._config
        if callable(self._config):
            return self._config()
        return settings()

    def flush(self) -> list[str]:
        """Open what is due and comment what recurred. Returns what it did; never raises."""
        with self._lock:
            try:
                return self._flush()
            except Exception as exc:  # noqa: BLE001 - reporting must never break serve
                log.warning("[deficiencies] Could not report to GitHub: %s", exc)
                return []

    def _flush(self) -> list[str]:
        config = self._settings()
        if not config.enabled:
            return []
        repo = runtime_repo(config, self._origin)
        if repo is None:
            if not self._warned:
                self._warned = True
                log.warning(
                    "[deficiencies] The runtime's origin is not a GitHub repository, so "
                    "self-reported deficiencies stay in the ledger (`ppy deficiency list`); "
                    "set self_report.repo to open them as issues"
                )
            return []
        from papaya_agent_runtime.state import init_db

        done: list[str] = []
        today = self._clock().astimezone(UTC).date().isoformat()
        conn = init_db()
        try:
            rows = [Deficiency.from_row(r) for r in conn.execute("SELECT * FROM deficiencies")]
            opened_today = sum(1 for d in rows if (d.opened_at or "").startswith(today))
            pending = sorted((d for d in rows if d.status == PENDING), key=lambda d: d.first_seen)
            for deficiency in pending:
                if opened_today >= config.max_per_day:
                    break
                url = self._forge.create_issue(
                    repo, deficiency.title, issue_body(deficiency), [LABEL, deficiency.kind]
                )
                if not url:
                    continue
                opened_today += 1
                conn.execute(
                    "UPDATE deficiencies SET status = ?, issue_url = ?, opened_at = ?, "
                    "reported_count = ? WHERE fingerprint = ?",
                    (
                        REPORTED,
                        url,
                        _stamp(self._clock),
                        deficiency.count,
                        deficiency.fingerprint,
                    ),
                )
                conn.commit()
                done.append(f"opened {url}")
            for deficiency in rows:
                if deficiency.status != REPORTED or not deficiency.issue_url:
                    continue
                if deficiency.count <= deficiency.reported_count:
                    continue
                new = [
                    e
                    for e in deficiency.evidence
                    if int(e.get("n") or 0) > deficiency.reported_count
                ]
                body = comment_body(deficiency, new)
                closed = (self._forge.state(deficiency.issue_url) or "") == "CLOSED"
                if closed:
                    ok = self._forge.reopen(deficiency.issue_url, body)
                else:
                    ok = self._forge.comment(deficiency.issue_url, body)
                if not ok:
                    continue
                conn.execute(
                    "UPDATE deficiencies SET reported_count = ?, status = ? WHERE fingerprint = ?",
                    (deficiency.count, REPORTED, deficiency.fingerprint),
                )
                conn.commit()
                done.append(("reopened " if closed else "commented on ") + deficiency.issue_url)
        finally:
            conn.close()
        for line in done:
            log.info("[deficiencies] %s", line)
        return done

    def flush_soon(self) -> None:
        """Flush on a thread of its own, now, coalescing records that arrive meanwhile."""
        with self._state:
            self._dirty = True
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._drain, name="ppy-self-report", daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        while True:
            with self._state:
                if not self._dirty:
                    self._thread = None
                    return
                self._dirty = False
            self.flush()

    def wait(self, timeout: float = 30.0) -> None:
        """Let a flush in progress finish (at shutdown, and in tests)."""
        with self._state:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)


# ── signals that need a little reading first ────────────────────────────────


def exception_detail(where: str, exc: BaseException) -> str:
    """``where``, the exception's type, and the innermost runtime frame it came from."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "papaya_agent_runtime" in f.filename] or frames
    at = f" in {Path(ours[-1].filename).stem}.{ours[-1].name}" if ours else ""
    return f"{where}: {type(exc).__name__}{at}"


def record_exception(
    where: str, exc: BaseException, *, scrub: Iterable[str] = (), **evidence: Any
) -> Deficiency | None:
    """An exception caught at the top of a loop, with its traceback. Never raises."""
    try:
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        detail = exception_detail(where, exc)
    except Exception:  # noqa: BLE001 - a traceback that cannot be read is still an exception
        text, detail = repr(exc), f"{where}: {type(exc).__name__}"
    return record(
        UNHANDLED_EXCEPTION,
        detail,
        evidence={**evidence, "where": where, "error": text},
        scrub=scrub,
    )


def _task_repo(conn: Any, task_id: int) -> str | None:
    row = conn.execute(
        "SELECT r.name FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    return str(row["name"]) if row is not None and row["name"] else None


def record_denials(
    denials: Iterable[dict[str, Any]], *, task_id: int, run_id: int | None, worktree: str | None
) -> None:
    """A worker turn's permission denials outside the safe family. Never raises.

    ``denials`` are the adapter's (`ProviderAdapter.permission_denials`): the same
    ones `tool_learning.learn` learns from. A denial inside the safe family is the
    runtime's to learn, not to report; one outside it is judged with the same
    `tool_learning.classify`, so the two can never disagree about which is which.
    """
    try:
        denials = [d for d in denials if isinstance(d, dict)]
        if not denials:
            return
        from papaya_agent_runtime.state import init_db

        conn = init_db()
        try:
            repo = _task_repo(conn, task_id)
        finally:
            conn.close()
        for denial in denials:
            tool = str(denial.get("tool_name") or denial.get("tool") or "")
            tool_input = denial.get("tool_input") or {}
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            verdict = tool_learning.classify(tool, command, worktree)
            pattern = verdict.pattern or tool
            if verdict.in_family or not pattern:
                continue
            record(
                WORKER_DENIAL,
                f"`{pattern}`",
                evidence={"pattern": pattern, "repo": repo, "task_id": task_id, "run_id": run_id},
                scope=f"repo:{repo or '?'}",
            )
    except Exception as exc:  # noqa: BLE001 - a worker's turn must end whatever this does
        log.warning("[deficiencies] Could not read task %s's denials: %s", task_id, exc)


def record_gate_past_tool_cap(task_id: int, run_id: int | None, command: str | None) -> None:
    """A worker stopped on a backgrounded command with no `ppy gate run` on record."""
    try:
        from papaya_agent_runtime import gate
        from papaya_agent_runtime.state import init_db

        conn = init_db()
        try:
            ran = conn.execute(
                "SELECT 1 FROM events WHERE task_id = ? AND kind = ? LIMIT 1",
                (task_id, gate.GATE_STARTED),
            ).fetchone()
            repo = _task_repo(conn, task_id)
        finally:
            conn.close()
        if ran is not None:
            return
        record(
            GATE_PAST_TOOL_CAP,
            "a backgrounded gate",
            evidence={"command": command, "repo": repo, "task_id": task_id, "run_id": run_id},
        )
    except Exception as exc:  # noqa: BLE001 - never breaks a worker's turn end
        log.warning("[deficiencies] Could not check task %s's gate: %s", task_id, exc)


__all__ = [
    "GATE_PAST_TOOL_CAP",
    "KINDS",
    "LABEL",
    "MISSED_TURN",
    "PENDING",
    "READINESS_UNREMEDIED",
    "REPEATED_STEER",
    "REPORTED",
    "RUNTIME_CI_RED",
    "STALL_WHILE_LIVE",
    "TURN_REPORT",
    "UNHANDLED_EXCEPTION",
    "WATCHING",
    "WORKER_DENIAL",
    "Deficiency",
    "GhForge",
    "Reporter",
    "Settings",
    "add_listener",
    "comment_body",
    "fingerprint",
    "github_slug",
    "issue_body",
    "ledger",
    "normalise",
    "notify",
    "record",
    "record_denials",
    "record_exception",
    "record_gate_past_tool_cap",
    "redact",
    "remove_listener",
    "runtime_repo",
    "settings",
    "summary",
]
