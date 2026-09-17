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
#: A worker was denied a plain command its profile could not be taught to allow.
WORKER_DENIAL = "worker-denial"
#: Workers in one repository kept breaking the command rules: the rules text is unclear.
PROMPT_CLARITY = "prompt-clarity"
#: An exception escaped `serve`, a turn, a round, the sweep or the supervisor.
UNHANDLED_EXCEPTION = "unhandled-exception"
#: A check-in steered a worker for the same reason again.
REPEATED_STEER = "repeated-steer"
#: CI went red on a pull request the runtime delivered to its own repository.
RUNTIME_CI_RED = "runtime-ci-red"
#: Papaya refused this machine the same idle item on three sweeps running.
IDLE_WORK_REFUSED = "idle-work-refused"
#: A turn did what its prompt tells it not to do for this agent (`propose_memory` on a shared one).
PROMPT_DEFECT = "prompt-defect"
#: A supervision capability works only under `ppy serve` (`parity`), so a session misses it.
SERVE_ONLY_CAPABILITY = "serve-only-capability"
#: `ppy deliver` pushed, and then `gh` refused to open or update the pull request.
DELIVERY_FAILED = "delivery-failed"

#: Ledger statuses: below its threshold; ready for an issue; an issue exists.
WATCHING = "watching"
PENDING = "pending"
REPORTED = "reported"
#: A `worker-denial` row whose denials a later classifier says were never profile gaps,
#: or a `turn-report` row about a check-in a later fix made impossible.
RECLASSIFIED = "reclassified"


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
    #: When set, the threshold counts distinct values of this evidence field instead.
    distinct: str = ""


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
        title="Workers were denied a plain command the runtime cannot learn",
        happened=(
            "Workers in one repository were refused {detail} more than once, for a plain "
            "command (one program, no operators). Either it is outside the safe family, so "
            "the runtime does not learn it, or the profile already allows it and the harness "
            "refused it anyway; every dispatch there meets the same refusal."
        ),
        instead="Learned nothing; the workers carried on without the tool.",
        remedy=(
            "Decide whether the pattern belongs in the worker profile, or whether the briefs "
            "for that repository should route around it."
        ),
        threshold=2,
    ),
    PROMPT_CLARITY: Kind(
        title="Workers keep breaking the command rules",
        happened=(
            "Workers in one repository ran commands the command rules refuse for their shape "
            "(operators, pipes, redirection, inline environment) on the same day: {detail}. "
            "Each worker read the rules at dispatch, so the rules text is not landing."
        ),
        instead=(
            "Recorded the denials, and steered each worker that was refused twice with the "
            "command rules once; no tool was added."
        ),
        remedy=(
            "Reword the command rules (`providers/command_rules.py`) where the commands in "
            "the evidence show they were misread."
        ),
        threshold=3,
        distinct="task_id",
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
    IDLE_WORK_REFUSED: Kind(
        title="Papaya kept refusing work nobody was doing",
        happened=(
            "The sweep asked for an item assigned to this agent on three sweeps running and "
            "Papaya refused it every time ({detail}), while the item had no live reservation, "
            "no live agent job and no word from its holder."
        ),
        instead=(
            "Kept asking every sweep and told the owner, through a blocker, to use Run on this "
            "Mac or wait for the guard to lift."
        ),
        remedy=(
            "Let a connection reclaim work its earlier connection held, and end a guard window "
            "that no hosted run is using."
        ),
    ),
    PROMPT_DEFECT: Kind(
        title="A turn did what its prompt tells it not to",
        happened=(
            "A manager turn's facts said what this agent cannot do, its prompt said what to "
            "do instead, and the turn tried it anyway: {detail}."
        ),
        instead="Nothing was lost to it: the refusal came back and the turn carried on.",
        remedy=(
            "Reword the prompt's instruction where the turn met it, or move the fact nearer "
            "the step that needs it."
        ),
    ),
    DELIVERY_FAILED: Kind(
        title="`ppy deliver` pushed, and the pull request step failed",
        happened=(
            "`ppy deliver` pushed the branch and then {detail}. `gh`'s own words are the "
            "error in the evidence."
        ),
        instead=(
            "Recorded the delivery with the push and `gh`'s error, and put the error in the "
            "delivery's note and the ticket's phase line."
        ),
        remedy=(
            "Read `gh`'s error: missing auth or permission is a blocker a person closes; "
            "anything else is a delivery bug."
        ),
    ),
    SERVE_ONLY_CAPABILITY: Kind(
        title="A supervision capability works only under ppy serve",
        happened=(
            "The runtime runs as `ppy serve` and as an interactive session and must supervise "
            "workers equally in both, but this capability is registered as serve-only in "
            "`papaya_agent_runtime.parity`: {detail}."
        ),
        instead=(
            "Serve does it; a session covers it by hand, and misses it when nobody remembers."
        ),
        remedy=(
            "Make it one decision both modes call, register it as shared in `parity`, prove it "
            "in both modes in tests, and remove it from `parity.KNOWN_GAPS`."
        ),
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


#: A tool or API a turn names: `propose_memory`, `Job.report_progress`, `ppy gate`.
_TOOL = re.compile(r"\bppy\s+[a-z][a-z-]*|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_ERROR_CLASS = re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b")
_REPO = re.compile(r"\b(?:repo|repository)\s+`?([A-Za-z0-9][\w.\-/]*[A-Za-z0-9])`?")
_WORD = re.compile(r"[a-z][a-z0-9_\-]*|[^\sa-z]")
_REFUSALS = ("refus", "reject", "denie", "deny", "forbid", "forbade", "disallow", "block")
_DETERMINERS = frozenset(
    ("a", "an", "the", "its", "their", "this", "that", "these", "those", "any", "every")
    + ("some", "to", "my", "our")
)
_STOPWORDS = _DETERMINERS | frozenset(
    ("about", "after", "again", "all", "also", "and", "are", "as", "at", "be", "because")
    + ("been", "before", "being", "but", "by", "can", "cannot", "could", "did", "do", "does")
    + ("for", "from", "had", "has", "have", "he", "her", "him", "his", "how", "i", "if", "in")
    + ("into", "is", "it", "me", "no", "nor", "not", "of", "off", "on", "once", "only", "or")
    + ("other", "out", "over", "own", "same", "she", "should", "so", "still", "such", "than")
    + ("then", "there", "they", "through", "too", "under", "until", "up", "very", "was", "we")
    + ("were", "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with")
    + ("would", "you", "your")
    # The placeholders `normalise` leaves behind: `<id>`, `<n>`, `<path>`.
    + ("id", "n", "path")
)
#: How many stemmed content words stand for a line that names no tool.
CONTENT_WORDS = 8


def _stem(word: str) -> str:
    """A crude suffix strip, enough that "refused" and "refuses" read as one word."""
    for suffix, keep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3 and not word.endswith("ss"):
            return word[: -len(suffix)] + keep
    return word


def _content_words(tokens: list[str], limit: int = CONTENT_WORDS) -> list[str]:
    words = [_stem(t) for t in tokens if t[0].isalpha() and t not in _STOPWORDS and len(t) > 1]
    return words[:limit]


def _refusal_noun(tokens: list[str]) -> str:
    """The noun phrase after the first refusal verb: "refused an agent-scoped proposal"."""
    for at, token in enumerate(tokens):
        if not token.startswith(_REFUSALS):
            continue
        phrase: list[str] = []
        for word in tokens[at + 1 :]:
            if not phrase and word in _DETERMINERS:
                continue
            if not word[0].isalpha() or word in _STOPWORDS or len(phrase) == 4:
                break
            phrase.append(_stem(word))
        if phrase:
            return " ".join(phrase)
    return ""


def reduce_turn_report(detail: str) -> str:
    """What a `RUNTIME:` line is about, not how the turn happened to word it.

    ``tool | error class or refusal noun phrase | repo``: the first tool or API the
    line names, the first exception class in it or else the noun phrase after its first
    refusal verb, and the repository when it names one. Two turns that hit the same
    refusal and say so in different sentences reduce to the same thing. A line that
    names no tool is its first eight stemmed content words, which still ignores what
    varies (ids, numbers, paths) and small rewordings around them.
    """
    text = _one_line(detail)
    error = _ERROR_CLASS.search(text)
    repo = _REPO.search(text)
    lowered = normalise(text)
    tokens = _WORD.findall(lowered)
    tool = _TOOL.search(lowered.replace("`", " "))
    if tool is None:
        return "words|" + " ".join(_content_words(tokens))
    tokens = _WORD.findall(lowered[tool.end() :].replace("`", " "))
    cause = error.group(0) if error else _refusal_noun(tokens)
    if not cause:
        cause = " ".join(_content_words(tokens, 4))
    where = repo.group(1).lower() if repo else ""
    return f"{' '.join(tool.group(0).split())}|{cause}|{where}"


def fingerprint(kind: str, detail: str) -> str:
    """A stable id for one deficiency: its kind and its normalised detail.

    A turn report's detail is a sentence a model wrote, so it is reduced to its
    cause first (:func:`reduce_turn_report`); every other kind's detail is written
    by the runtime and only normalised.
    """
    reduced = reduce_turn_report(detail) if kind == TURN_REPORT else normalise(detail)
    return hashlib.sha256(f"{kind}\n{reduced}".encode()).hexdigest()[:16]


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
            seen = len({e.get(spec.distinct) for e in within}) if spec.distinct else len(within)
            if seen >= spec.threshold:
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
    return found if include_all else [d for d in found if d.status not in (WATCHING, RECLASSIFIED)]


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

    def close(self, url: str, body: str) -> bool:
        code, _out, err = self._gh(["issue", "close", url, "--comment", body])
        if code != 0:
            log.warning("[deficiencies] gh could not close %s: %s", url, err.strip())
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

    def reclassify(self) -> list[str]:
        """Close what a later classifier or fix says is no longer the runtime's. Never raises.

        `serve` runs this at start. Each `worker-denial` row whose denials all classify
        as `command_shape` or `policy_refusal` today leaves the ledger, and so does each
        `turn-report` row whose every occurrence came from a check-in a later fix made
        impossible (:func:`fixed_checkin`) or that a named fix answered before it landed
        (:func:`fixed_turn_report`); if it has an open issue, the issue gets one
        comment saying so and is closed. A row whose issue cannot be closed now (no
        `gh`, self-reporting off) stays as it is and is tried again at the next start.
        """
        with self._lock:
            try:
                return self._reclassify()
            except Exception as exc:  # noqa: BLE001 - reporting must never break serve
                log.warning("[deficiencies] Could not re-classify worker denials: %s", exc)
                return []

    def _reclassify(self) -> list[str]:
        from papaya_agent_runtime.paths import db_path
        from papaya_agent_runtime.state import init_db

        if not db_path().exists():
            return []
        done: list[str] = []
        repo: str | None = None
        conn = init_db()
        try:
            rows = [
                Deficiency.from_row(r)
                for r in conn.execute(
                    "SELECT * FROM deficiencies WHERE kind IN (?, ?) AND status != ?",
                    (WORKER_DENIAL, TURN_REPORT, RECLASSIFIED),
                )
            ]
            for deficiency in rows:
                if deficiency.kind == TURN_REPORT:
                    body = fixed_checkin(conn, deficiency) or fixed_turn_report(deficiency)
                    if body is None:
                        continue
                else:
                    kinds = denial_kinds(conn, deficiency)
                    if not kinds or tool_learning.PROFILE_GAP in kinds:
                        continue
                    body = reclassified_body(kinds)
                if deficiency.status == REPORTED and deficiency.issue_url:
                    if repo is None:
                        config = self._settings()
                        repo = runtime_repo(config, self._origin) if config.enabled else None
                    if repo is None:
                        continue
                    url = deficiency.issue_url
                    if (self._forge.state(url) or "") != "CLOSED":
                        if not self._forge.close(url, body):
                            continue
                        done.append(f"closed {url}")
                conn.execute(
                    "UPDATE deficiencies SET status = ? WHERE fingerprint = ?",
                    (RECLASSIFIED, deficiency.fingerprint),
                )
                conn.commit()
        finally:
            conn.close()
        for line in done:
            log.info("[deficiencies] %s (re-classified)", line)
        return done

    def merge_duplicates(self) -> list[str]:
        """Fold turn-report rows that reduce to one cause into one. Never raises.

        `serve` runs this at start. A turn report recorded before fingerprints were
        reduced (:func:`reduce_turn_report`) sits under the fingerprint of its whole
        sentence, so two wordings of one cause are two rows and two issues. Each group
        that now shares a fingerprint keeps the row with the earliest issue; every
        other open issue in the group gets one comment, "duplicate of #N", and is
        closed, and its occurrences join the kept row. A duplicate whose issue cannot
        be closed now stays as it is and is tried again at the next start.
        """
        with self._lock:
            try:
                return self._merge_duplicates()
            except Exception as exc:  # noqa: BLE001 - reporting must never break serve
                log.warning("[deficiencies] Could not merge duplicate turn reports: %s", exc)
                return []

    def _merge_duplicates(self) -> list[str]:
        from papaya_agent_runtime.paths import db_path
        from papaya_agent_runtime.state import init_db

        if not db_path().exists():
            return []
        done: list[str] = []
        repo: str | None = None
        conn = init_db()
        try:
            rows = [
                Deficiency.from_row(r)
                for r in conn.execute(
                    "SELECT * FROM deficiencies WHERE kind = ? AND status != ?",
                    (TURN_REPORT, RECLASSIFIED),
                )
            ]
            groups: dict[str, list[Deficiency]] = {}
            for deficiency in rows:
                groups.setdefault(fingerprint(TURN_REPORT, deficiency.detail), []).append(
                    deficiency
                )
            for key, group in groups.items():
                if len(group) == 1 and group[0].fingerprint == key:
                    continue
                group.sort(key=_merge_order)
                kept, rest = group[0], group[1:]
                merged: list[Deficiency] = []
                for duplicate in rest:
                    url = duplicate.issue_url
                    if duplicate.status == REPORTED and url and url != kept.issue_url:
                        if repo is None:
                            config = self._settings()
                            repo = runtime_repo(config, self._origin) if config.enabled else None
                        if repo is None:
                            continue
                        if (self._forge.state(url) or "") != "CLOSED":
                            if not self._forge.close(url, duplicate_body(kept)):
                                continue
                            done.append(f"closed {url} as a duplicate of {kept.issue_url}")
                    merged.append(duplicate)
                if any(d.fingerprint == key for d in rest if d not in merged):
                    continue  # the new key is taken by a duplicate still open; next start
                _fold(conn, key, kept, merged)
                conn.commit()
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


_ISSUE_NUMBER = re.compile(r"/issues/(\d+)\b")


def issue_number(url: str | None) -> int | None:
    match = _ISSUE_NUMBER.search(str(url or ""))
    return int(match.group(1)) if match else None


def _merge_order(deficiency: Deficiency) -> tuple[int, int, str]:
    """The row a group keeps sorts first: an issue before none, the lowest number first."""
    number = issue_number(deficiency.issue_url)
    return (0 if number is not None else 1, number or 0, deficiency.first_seen)


def duplicate_body(kept: Deficiency) -> str:
    """The one comment a duplicate issue gets as it is closed."""
    number = issue_number(kept.issue_url)
    target = f"#{number}" if number is not None else (kept.issue_url or "the kept report")
    return (
        f"duplicate of {target}\n\n"
        "The runtime now fingerprints a turn's `RUNTIME:` line by its cause (the tool it "
        "names, the error or refusal, the repository) rather than its wording, and this "
        "report has the same cause as that one. Its occurrences are counted there."
    )


def _fold(conn: Any, key: str, kept: Deficiency, merged: list[Deficiency]) -> None:
    """Make ``kept`` the row for ``key``, with ``merged``'s occurrences, and drop the rest."""
    group = (kept, *merged)
    count = sum(d.count for d in group)
    entries = sorted((e for d in group for e in d.evidence), key=lambda e: str(e.get("at") or ""))[
        -EVIDENCE_KEPT:
    ]
    # Occurrence numbers run on across the group, so a recurrence comments only what is new.
    for n, entry in enumerate(entries, start=count - len(entries) + 1):
        entry["n"] = n
    if kept.issue_url:
        # What the duplicates' issues said is said; only what comes next is news.
        status, reported = kept.status, count
    else:
        status = PENDING if any(d.status == PENDING for d in group) else kept.status
        reported = 0
    for gone in {d.fingerprint for d in group}:
        conn.execute("DELETE FROM deficiencies WHERE fingerprint = ?", (gone,))
    conn.execute(
        "INSERT INTO deficiencies (fingerprint, kind, title, detail, first_seen, last_seen, "
        "count, evidence, issue_url, status, opened_at, reported_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            kept.kind,
            kept.title,
            kept.detail,
            min(d.first_seen for d in group),
            max(d.last_seen for d in group),
            count,
            json.dumps(entries),
            kept.issue_url,
            status,
            kept.opened_at,
            reported,
        ),
    )


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


#: The detail of the one `prompt-clarity` deficiency about the command rules.
COMMAND_RULES_DETAIL = "the command rules"


def record_denials(
    denials: Iterable[dict[str, Any]],
    *,
    task_id: int,
    run_id: int | None,
    worktree: str | None,
    profile: Iterable[str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """What a worker's new denials mean for the ledger, by kind. Never raises.

    ``denials`` are the adapter's (`ProviderAdapter.permission_denials`), already
    deduplicated by `tool_learning.learn`, and judged with the same
    `tool_learning.classify`, so the two never disagree about which is which:

    - ``profile_gap``: a `worker-denial` when learning cannot close it, that is when
      the program is outside the safe family, or ``profile`` (the tools the worker
      was dispatched with, when known) already allowed the pattern.
    - ``command_shape``: one `prompt-clarity` occurrence per worker, scoped to its
      repository and the day, with the command as evidence.
    - ``policy_refusal``: nothing; `tool_learning` counts it and steers the worker.
    """
    try:
        denials = [d for d in denials if isinstance(d, dict)]
        if not denials:
            return
        from papaya_agent_runtime.state import init_db

        allowed = set(profile) if profile is not None else None
        conn = init_db()
        try:
            repo = _task_repo(conn, task_id)
        finally:
            conn.close()
        day = (clock or _now)().astimezone(UTC).date().isoformat()
        for denial in denials:
            tool = str(denial.get("tool_name") or denial.get("tool") or "")
            tool_input = denial.get("tool_input") or {}
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            verdict = tool_learning.classify(tool, command, worktree)
            evidence = {"repo": repo, "task_id": task_id, "run_id": run_id, "command": command}
            if verdict.kind == tool_learning.COMMAND_SHAPE:
                scope = f"repo:{repo or '?'}:{day}"
                if not _counted(PROMPT_CLARITY, COMMAND_RULES_DETAIL, scope, task_id):
                    record(PROMPT_CLARITY, COMMAND_RULES_DETAIL, evidence=evidence, scope=scope)
                continue
            if verdict.kind != tool_learning.PROFILE_GAP:
                continue
            pattern = verdict.pattern or tool
            if not pattern:
                continue
            if verdict.in_family and (allowed is None or pattern not in allowed):
                continue  # the runtime learns this one
            record(
                WORKER_DENIAL,
                f"`{pattern}`",
                evidence={**evidence, "pattern": pattern},
                scope=f"repo:{repo or '?'}",
            )
    except Exception as exc:  # noqa: BLE001 - a worker's turn must end whatever this does
        log.warning("[deficiencies] Could not read task %s's denials: %s", task_id, exc)


def _counted(kind: str, detail: str, scope: str, task_id: int) -> bool:
    """Whether this worker is already an occurrence of the deficiency within ``scope``."""
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    try:
        row = conn.execute(
            "SELECT evidence FROM deficiencies WHERE fingerprint = ?", (fingerprint(kind, detail),)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    try:
        entries = json.loads(row["evidence"] or "[]")
    except (TypeError, ValueError):
        return False
    return any(e.get("scope") == scope and e.get("task_id") == task_id for e in entries)


def denial_kinds(conn: Any, deficiency: Deficiency) -> set[str]:
    """The kinds today's classifier gives a `worker-denial` row's denials.

    A row written before kinds existed carries only the pattern, so its commands are
    read back from the `permission_denied` events of the tasks in its evidence. An
    empty set means nothing could be read, which is never a reason to close an issue.
    """
    pattern = next(
        (str(e["pattern"]) for e in deficiency.evidence if e.get("pattern")),
        deficiency.detail.strip("`"),
    )
    kinds: set[str] = set()
    task_ids: set[int] = set()
    for entry in deficiency.evidence:
        if isinstance(entry.get("task_id"), int):
            task_ids.add(int(entry["task_id"]))
        if entry.get("command") and pattern.startswith("Bash("):
            kinds.add(tool_learning.classify("Bash", str(entry["command"]), None).kind)
    for task_id in sorted(task_ids):
        rows = conn.execute(
            "SELECT payload FROM events WHERE kind = ? AND task_id = ?",
            (tool_learning.PERMISSION_DENIED, task_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if payload.get("pattern") == pattern:
                kinds.add(tool_learning.kind_of(payload))
    return kinds


def reclassified_body(kinds: Iterable[str]) -> str:
    """The one comment a re-classified issue gets as it is closed."""
    names = "/".join(sorted(kinds))
    return (
        f"re-classified as {names}; closing.\n\n"
        "The runtime now tells a denial's kind apart. `command_shape` is a command the "
        "command rules refuse for its shape (operators, pipes, redirection, inline "
        "environment), and `policy_refusal` is a program workers are never given. Neither "
        "is a tool missing from the worker profile: the worker is steered with the rule "
        "instead, and only `profile_gap` denials open issues."
    )


@dataclass(frozen=True)
class CheckinFix:
    """A fix to one check-in trigger, as the ledger can recognise what came before it."""

    #: A field every record of the trigger has carried since the fix; a record of the
    #: trigger without it was made by the code the fix replaced.
    marker: str
    #: What the fix changed, for the one comment on the issue it closes.
    note: str


#: Check-in triggers a fix has changed, so a turn's report that one of them was wrong
#: before the fix is closed from the fix rather than left open.
FIXED_CHECKINS: dict[str, CheckinFix] = {
    "push": CheckinFix(
        marker="head_sha",
        note=(
            'The push check-in used to measure "nothing pushed" from the commit date of the '
            "remote-tracking ref's tip, not from when that tip reached the forge, so a push "
            "that landed minutes ago read as the commit's age. It now asks the forge with "
            "`git ls-remote` every round, records when a round first sees a new tip, compares "
            "that tip with the worktree's HEAD, and never fires for a HEAD that is on the "
            "forge. Its record and the check-in facts carry the forge's tip, the HEAD and the "
            "last recorded push."
        ),
    ),
}


def fixed_checkin(conn: Any, deficiency: Deficiency) -> str | None:
    """The closing comment for a `turn-report` about a check-in a later fix made impossible.

    Every occurrence must come from a check-in turn, and the round record that started
    that check-in (the newest `checkin` for its worker at or before the occurrence)
    must name only triggers in :data:`FIXED_CHECKINS` and lack each one's marker. An
    occurrence the ledger cannot tie to such a record keeps the issue open.
    """
    if deficiency.kind != TURN_REPORT or not deficiency.evidence:
        return None
    notes: list[str] = []
    for entry in deficiency.evidence:
        task_id, worker, upto = (
            entry.get("task_id"),
            entry.get("worker_task_id"),
            entry.get("event_id"),
        )
        if entry.get("turn") != "checkin" or not all(
            isinstance(v, int) for v in (task_id, worker, upto)
        ):
            return None
        row = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'ticket_round' AND id <= ? "
            "AND json_valid(payload) AND json_extract(payload, '$.action') = 'checkin' "
            "AND json_extract(payload, '$.worker_task_id') = ? ORDER BY id DESC LIMIT 1",
            (task_id, upto, worker),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        fixes = [FIXED_CHECKINS.get(t) for t in str(payload.get("trigger") or "").split(",")]
        if not fixes or any(fix is None or fix.marker in payload for fix in fixes):
            return None
        notes += [fix.note for fix in fixes if fix is not None and fix.note not in notes]
    return (
        "closing: the check-in this turn reported on cannot fire that way any more.\n\n"
        + "\n\n".join(notes)
    )


@dataclass(frozen=True)
class ReportFix:
    """A fix to what one `turn-report` row described, as the ledger can recognise it."""

    #: The row's fingerprint under every scheme it may be keyed by: the normalised
    #: detail, and the reduced turn-report form (`tool|error|repo`) rows are rekeyed to.
    fingerprints: frozenset[str]
    #: Occurrences at or before this moment came from the code the fix replaced; a row
    #: with a later one is a recurrence and stays open.
    before: str
    #: What the fix changed, for the one comment on the issue it closes.
    note: str


_PAP_222_FIXED_AT = "2026-09-17T02:00:00+00:00"

#: `turn-report` rows a fix has answered, closed from the fix at `serve` start.
FIXED_TURN_REPORTS: tuple[ReportFix, ...] = (
    ReportFix(
        # "`ppy review show 21` compared against an older starting commit (3f3c0181)"
        fingerprints=frozenset({"74fccc066045a150", "af2aa9532fde4fce"}),
        before=_PAP_222_FIXED_AT,
        note=(
            "`ppy review show` diffed from the task's dispatch-time base, so a branch rebased "
            "onto a newer main showed main's commits as the worker's. It now fetches the "
            "branch the pull request targets and diffs from HEAD's merge-base with it, and "
            "names the base it used; the review turn gets the same range as a fact."
        ),
    ),
    ReportFix(
        # "task 21's worktree slot had been reset to the base commit after delivery"
        fingerprints=frozenset({"ecfb07ec73b22d9a", "7ba58c5a7c72a5b0"}),
        before=_PAP_222_FIXED_AT,
        note=(
            "Hygiene removed a delivered task's worktree while its pull request was open, and "
            "the reconcile lane rebuilt it at the base commit. A delivered task's slot is now "
            "kept while its pull request is open (`kept: PR #N open`), and a rebuilt worktree "
            "for a delivered task is checked out at the pull request's head from the forge, "
            "never at the base."
        ),
    ),
    ReportFix(
        # "`ppy deliver 21` reported "PR creation failed" with no reason"
        fingerprints=frozenset({"73e968fea9ac0d8e", "c4b7a8dab0a58df5"}),
        before=_PAP_222_FIXED_AT,
        note=(
            "`ppy deliver` always tried to create a pull request and cut `gh`'s refusal "
            "short. It now looks for the open pull request on the branch first and updates "
            "it (`PR #N updated`), and a genuine failure carries `gh`'s error verbatim in the "
            "delivery note, the ticket's phase line and a `delivery-failed` deficiency."
        ),
    ),
)


def fixed_turn_report(deficiency: Deficiency) -> str | None:
    """The closing comment for a `turn-report` row a fix in :data:`FIXED_TURN_REPORTS` answers."""
    if deficiency.kind != TURN_REPORT or not deficiency.evidence:
        return None
    for fix in FIXED_TURN_REPORTS:
        if deficiency.fingerprint not in fix.fingerprints:
            continue
        cutoff = datetime.fromisoformat(fix.before)
        for entry in deficiency.evidence:
            try:
                at = datetime.fromisoformat(str(entry.get("at") or ""))
            except ValueError:
                return None
            if at.tzinfo is None or at > cutoff:
                return None
        return "closing: fixed.\n\n" + fix.note
    return None


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
    "DELIVERY_FAILED",
    "FIXED_CHECKINS",
    "FIXED_TURN_REPORTS",
    "GATE_PAST_TOOL_CAP",
    "KINDS",
    "LABEL",
    "MISSED_TURN",
    "PENDING",
    "PROMPT_CLARITY",
    "PROMPT_DEFECT",
    "READINESS_UNREMEDIED",
    "RECLASSIFIED",
    "REPEATED_STEER",
    "REPORTED",
    "RUNTIME_CI_RED",
    "STALL_WHILE_LIVE",
    "TURN_REPORT",
    "UNHANDLED_EXCEPTION",
    "WATCHING",
    "WORKER_DENIAL",
    "CheckinFix",
    "Deficiency",
    "GhForge",
    "Reporter",
    "Settings",
    "add_listener",
    "comment_body",
    "denial_kinds",
    "duplicate_body",
    "fingerprint",
    "fixed_checkin",
    "github_slug",
    "issue_body",
    "issue_number",
    "ledger",
    "normalise",
    "notify",
    "record",
    "record_denials",
    "record_exception",
    "record_gate_past_tool_cap",
    "reclassified_body",
    "redact",
    "reduce_turn_report",
    "remove_listener",
    "runtime_repo",
    "settings",
    "summary",
]
