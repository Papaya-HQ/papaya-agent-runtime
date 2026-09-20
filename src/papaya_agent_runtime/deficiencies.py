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

The channel is only worth reading if its open issues are true, so three rules bound
what it may say. **Nothing stale opens**: a row whose newest evidence predates the
running build, or is older than :data:`STALE_AFTER_SECONDS`, stopped happening
before this version existed and waits in the ledger as :data:`STALE` until it
happens again — which is what stops a backlog of old rows opening issues for weeks
after the fix. **One cause is one issue**: a turn's `RUNTIME:` line is fingerprinted
by what it names, not how it words it (:func:`reduce_turn_report`), and rows that
turn out to be one cause are folded together with the rest of their fingerprints
kept as aliases. **An issue that is over closes itself**: quiet for
:data:`QUIET_BEFORE_CLOSE_SECONDS` across a newer build, or belonging to a kind
another kind has replaced (:attr:`Kind.superseded_by`), it gets one comment and
closes; a recurrence reopens it.

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
#: Papaya refused this machine the same idle item on three sweeps running. Kept for the
#: rows already in ledgers; the sweep records :data:`REPEATED_WITHOUT_PROGRESS` instead,
#: once per ticket and reason, where this kind was one occurrence per streak.
IDLE_WORK_REFUSED = "idle-work-refused"
#: A turn did what its prompt tells it not to do for this agent (`propose_memory` on a shared one).
PROMPT_DEFECT = "prompt-defect"
#: A supervision capability works only under `ppy serve` (`parity`), so a session misses it.
SERVE_ONLY_CAPABILITY = "serve-only-capability"
#: `ppy deliver` pushed, and then `gh` refused to open or update the pull request.
DELIVERY_FAILED = "delivery-failed"
#: The lifeline watcher that stops a dead supervisor's workers could not start, or had exited.
LIFELINE_DOWN = "lifeline-down"
#: One ticket kept coming back with nothing to show: picked up again and again without
#: reaching a worker, or refused on sweep after sweep for the same reason.
REPEATED_WITHOUT_PROGRESS = "repeated-without-progress"

#: Kinds whose detail names one subject (`PAP-210`), fingerprinted on the detail as
#: written: normalising would turn every ticket's number into the same `<n>`.
EXACT_KINDS = frozenset({REPEATED_WITHOUT_PROGRESS})

#: Ledger statuses: below its threshold; ready for an issue; an issue exists.
WATCHING = "watching"
PENDING = "pending"
REPORTED = "reported"
#: A `worker-denial` row whose denials a later classifier says were never profile gaps,
#: or a `turn-report` row about a check-in a later fix made impossible.
RECLASSIFIED = "reclassified"
#: Due an issue, but its newest evidence is older than the running build or than
#: :data:`STALE_AFTER_SECONDS`: it stopped happening before this version existed, so
#: it stays in the ledger and opens nothing. One more occurrence makes it `pending`.
STALE = "stale"
#: A row of a kind another kind has replaced: its issue points at the successor's and closes.
SUPERSEDED = "superseded"

#: How old a row's newest evidence may be and still open an issue.
STALE_AFTER_SECONDS = 48 * 3600
#: How long a reported deficiency must go unseen before the runtime closes its issue.
QUIET_BEFORE_CLOSE_SECONDS = 7 * 24 * 3600
#: How long a refused close waits before it is tried again. A forge that is down, or an
#: issue somebody has locked, must not cost a `gh` call on every flush for ever.
CLOSE_RETRY_AFTER_SECONDS = 6 * 3600


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
    #: The kind that replaced this one. A row of a retired kind opens nothing, and its
    #: open issue gets one comment pointing at the successor's and is closed.
    superseded_by: str = ""


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
        superseded_by=REPEATED_WITHOUT_PROGRESS,
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
    LIFELINE_DOWN: Kind(
        title="The supervisor's lifeline watcher was not running",
        happened=(
            "The watcher that stops a supervisor's workers when the supervisor dies abruptly "
            "was not running: {detail}. Until it runs, an abrupt supervisor death leaves its "
            "workers running with nobody answering for them."
        ),
        instead=(
            "Tried to start it again and handed it every live runner's process group; a "
            "manager turn's or gate's registration made while it was down is not replayed."
        ),
        remedy=(
            "Find what ended the watcher (the error in the evidence, the supervisor log) and "
            "stop it happening; a watcher that cannot start at all is an environment defect."
        ),
    ),
    REPEATED_WITHOUT_PROGRESS: Kind(
        title="A ticket kept coming back with nothing to show",
        happened=(
            "The same ticket came round again and again without the work moving: {detail}. "
            "Each attempt looked like the first, so nothing said the runtime was repeating "
            "itself."
        ),
        instead=(
            "Recorded it once for this ticket and ending, and named it under `needs attention` "
            "in `ppy workers` and `ppy status --team`; the attempts themselves went on."
        ),
        remedy=(
            "Read the ending in the evidence: an ending the runtime forgets (it releases and "
            "the sweep offers the ticket again) needs remembering, and a refusal nobody can "
            "lift here needs a person or a routing change."
        ),
        # One stuck ticket is a `needs attention` line, not an issue: a second episode
        # (the same loop another day, or a second ticket refused the same way) is.
        threshold=2,
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
    "times",
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
#: An exception class, however a turn capitalised it (`AttributeError`, `attributeerror`).
#: At least one letter before `Error`/`Exception`, so the bare words do not match.
_ERROR_CLASS = re.compile(r"(?i)\b[a-z]+(?:error|exception)\b")
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
#: A pull request the line names: `PR #58`, `PR 58`, `pull request 710`. A number is only
#: a report when `#` or the word for one stands in front of it — "issue 3 of 5 checks" and
#: "ran 12 issues 4 times" name no report.
_NAMED_PR = re.compile(r"(?i)\b(?:prs?|pull\s+requests?)\s*#?\s*(\d{1,6})\b")
#: `#58`, anywhere. Guarded by :data:`_ENUMERATED`, which is what a bare number after a
#: ticket id or another number is ("PAP-219 #3").
_HASH_NUMBER = re.compile(r"#(\d{1,6})\b")
_ENUMERATED = re.compile(r"[\w\-]*\d+\s*$")
#: Where an exception came from: `claude.live_denial`, `serve.take`, `state/db.py`, `ppy gate`.
_WHERE_IDENT = re.compile(r"\bppy\s+[a-z][a-z-]*|\b[a-z][a-z0-9]*(?:[._][a-z0-9_]+)+\b")
#: An exception's own message: what it brackets, or what follows the class to the end of
#: the sentence. Whole, never a token of it — "'NoneType' object has no attribute 'get'"
#: and "'NoneType' object has no attribute 'phase'" are two causes.
_BRACKETED = re.compile(r"^\s*[:\-]?\s*\((.+?)\)")
_SENTENCE_END = re.compile(r"[.;](?:\s|$)")
#: How much of the text after an exception class its message may come from.
MESSAGE_CHARS = 200


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


def _named_number(text: str) -> str:
    """The pull request or issue number the line names, or ``""``.

    ``PR #58``, ``PR 58`` and ``pull request 710`` name one; so does a bare ``#94``,
    unless it is an enumeration rather than a report — a ``#3`` right after a ticket id
    or another number ("PAP-219 #3", "task 25 #2") is counted by something, not named.
    """
    named = _NAMED_PR.search(text)
    if named is not None:
        return named.group(1)
    for match in _HASH_NUMBER.finditer(text):
        if not _ENUMERATED.search(text[: match.start()]):
            return match.group(1)
    return ""


def _broke_in(text: str) -> str:
    """The function, module or command the line names, or ``""``.

    Anywhere in the line (``claude.live_denial``, ``serve.take``, ``ppy gate``), because
    a turn writes it as often before the exception class as after it.
    """
    ident = _WHERE_IDENT.search(normalise(text).replace("`", " "))
    return " ".join(ident.group(0).split()) if ident is not None else ""


def _exception_message(after: str) -> str:
    """An exception's message, whole: what it brackets, else the rest of the sentence.

    Whole, and never a token of it. "'NoneType' object has no attribute 'get'" and
    "'NoneType' object has no attribute 'phase' when parking" are two deficiencies, and
    a key built from the first quoted word would make them one — a bare class linking
    two unrelated rows, which is the thing this fingerprint exists to prevent.
    """
    said = after[:MESSAGE_CHARS]
    bracketed = _BRACKETED.match(said)
    if bracketed is not None:
        said = bracketed.group(1)
    else:
        end = _SENTENCE_END.search(said)
        said = said[: end.start()] if end is not None else said
    # Quotes and backticks are how a turn marks the message, not part of it.
    return " ".join(normalise(said).replace("`", " ").replace("'", " ").replace('"', " ").split())


def reduce_turn_report(detail: str) -> str:
    """What a `RUNTIME:` line is about, not how the turn happened to word it.

    One cause must reduce to one key however many ways turns word it, and two causes
    must never share one. So the key is the strongest thing the line names, by
    precedence, and two lines are the same cause only when they produce the same key:

    1. ``<exceptionclass>|<where>|<repo>``: an exception class *together with* the
       function, module or command the line names. This is first because it is the most
       specific thing a turn can say, and a pull request it mentions in passing must not
       take it over.
    2. ``pr|<number>|<repo>``: a pull request the line names, with the repository when it
       names one — two repositories' PR #58 are two things. A turn that says which change
       its trouble is about, and nowhere it came from, has said what the trouble is.
    3. ``<exceptionclass>|<message>|<repo>``: an exception class with nowhere named, keyed
       on its whole message.
    4. ``<tool>|<refusal or first words after it>|<repo>``: the first tool or API the
       line names, with the noun phrase after its first refusal verb.
    5. ``words|<first eight stemmed content words>``: a line that names none of those.

    This under-merges on purpose. Of the nine issues one crash produced in September,
    six name PR #58 and collapse; the two that name neither that pull request nor a
    shared location (#103, which quotes only the message, and #104, which names
    `claude.live_denial`) stay apart, which is the right side to err on.
    """
    text = _one_line(detail)
    repo = _REPO.search(text)
    where = repo.group(1).lower() if repo else ""
    error = _ERROR_CLASS.search(text)
    broke_in = _broke_in(text) if error is not None else ""
    if error is not None and broke_in:
        return f"{error.group(0).lower()}|{broke_in}|{where}"
    number = _named_number(text)
    if number:
        return f"pr|{number}|{where}"
    if error is not None:
        return f"{error.group(0).lower()}|{_exception_message(text[error.end() :])}|{where}"
    lowered = normalise(text)
    tokens = _WORD.findall(lowered)
    tool = _TOOL.search(lowered.replace("`", " "))
    if tool is None:
        return "words|" + " ".join(_content_words(tokens))
    tokens = _WORD.findall(lowered[tool.end() :].replace("`", " "))
    cause = _refusal_noun(tokens) or " ".join(_content_words(tokens, 4))
    return f"{' '.join(tool.group(0).split())}|{cause}|{where}"


def fingerprint(kind: str, detail: str) -> str:
    """A stable id for one deficiency: its kind and its normalised detail.

    A turn report's detail is a sentence a model wrote, so it is reduced to its
    cause first (:func:`reduce_turn_report`); every other kind's detail is written
    by the runtime and only normalised.
    """
    if kind == TURN_REPORT:
        reduced = reduce_turn_report(detail)
    elif kind in EXACT_KINDS:
        reduced = _one_line(detail).lower()
    else:
        reduced = normalise(detail)
    return hashlib.sha256(f"{kind}\n{reduced}".encode()).hexdigest()[:16]


# ── the ledger ──────────────────────────────────────────────────────────────


def _column(row: Any, name: str) -> str | None:
    """``row[name]``, or ``None`` when the row was read before that column existed."""
    try:
        return row[name] or None
    except (IndexError, KeyError):
        return None


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
    #: When the runtime closed the issue itself; cleared when a recurrence reopens it.
    closed_at: str | None = None
    #: When the runtime last tried to close it, whether or not the forge let it.
    close_tried_at: str | None = None

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
            closed_at=_column(row, "closed_at"),
            close_tried_at=_column(row, "close_tried_at"),
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


def record_once(
    kind: str,
    detail: str,
    *,
    within: float,
    evidence: Mapping[str, Any] | None = None,
    scope: str | None = None,
    scrub: Iterable[str] = (),
    clock: Callable[[], datetime] | None = None,
    per: str = "",
) -> Deficiency | None:
    """:func:`record`, unless this fingerprint was already seen in the last ``within`` seconds.

    For a signal that stays true while the thing it names goes on repeating: one
    occurrence per episode, not one per repetition. ``per`` names an evidence field
    (`ticket`) whose value gets its own window inside one fingerprint, so one row can
    gather every ticket stuck the same way, each once. Returns ``None`` when skipped.
    Never raises.
    """
    try:
        key = fingerprint(kind, _clean_detail(detail, list(scrub)))
        now = (clock or _now)()
        wanted = redact((evidence or {}).get(per), list(scrub)) if per else None
        for at in _seen_at(key, per, wanted):
            if (now - at).total_seconds() < within:
                return None
    except Exception as exc:  # noqa: BLE001 - reporting the runtime must never break it
        log.warning("[deficiencies] Could not read the %s ledger row: %s", kind, exc)
        return None
    return record(kind, detail, evidence=evidence, scope=scope, scrub=scrub, clock=clock)


def _seen_at(key: str, per: str, wanted: object) -> list[datetime]:
    """When this fingerprint was recorded: its last-seen, or each occurrence for ``per``."""
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.state import init_db

    if not db_path().exists():
        return []
    conn = init_db()
    try:
        row = conn.execute(
            "SELECT last_seen, evidence FROM deficiencies WHERE fingerprint = ?",
            (canonical_fingerprint(conn, key),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return []
    if per:
        try:
            entries = json.loads(row["evidence"] or "[]")
        except ValueError:
            entries = []
        stamps = [str(e.get("at")) for e in entries if isinstance(e, dict) and e.get(per) == wanted]
    else:
        stamps = [str(row["last_seen"])]
    found = []
    for stamp in stamps:
        try:
            seen = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        found.append(seen if seen.tzinfo is not None else seen.replace(tzinfo=UTC))
    return found


def _clean_detail(detail: str, scrub: list[str]) -> str:
    return _one_line(redact(detail, scrub))[:300] or "(no detail)"


def canonical_fingerprint(conn: Any, key: str) -> str:
    """The row ``key`` belongs to: itself, or the one an alias points it at.

    A rule change moves a wording from one fingerprint to another. The ledger keeps a
    row under the fingerprint its issue was opened with, and every other fingerprint
    that means the same deficiency is an alias to it, so the wording that used to open
    its own issue comments the existing one instead.
    """
    row = conn.execute(
        "SELECT canonical FROM deficiency_aliases WHERE fingerprint = ?", (key,)
    ).fetchone()
    return str(row["canonical"]) if row is not None and row["canonical"] else key


def remember_alias(conn: Any, key: str, canonical: str, at: str) -> None:
    """Point ``key`` at ``canonical``, and move any alias that pointed at ``key``."""
    if not key or key == canonical:
        return
    conn.execute("DELETE FROM deficiency_aliases WHERE fingerprint = ?", (canonical,))
    conn.execute(
        "UPDATE deficiency_aliases SET canonical = ?, at = ? WHERE canonical = ?",
        (canonical, at, key),
    )
    conn.execute(
        "INSERT OR REPLACE INTO deficiency_aliases (fingerprint, canonical, at) VALUES (?, ?, ?)",
        (key, canonical, at),
    )


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
    clean = _clean_detail(detail, scrub)
    at = _stamp(clock)
    entry = {"at": at, **_clean_evidence(evidence, scrub)}
    if scope:
        entry["scope"] = redact(scope, scrub)
    conn = init_db()
    try:
        key = canonical_fingerprint(conn, fingerprint(kind, clean))
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
            # It is happening again on this build: a row put aside as stale is due an
            # issue after all, and the next flush opens it.
            status = PENDING if current.status == STALE else current.status
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
    if include_all:
        return found
    hidden = (WATCHING, RECLASSIFIED, SUPERSEDED)
    return [d for d in found if d.status not in hidden]


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

    def close(self, url: str, body: str = "") -> bool:
        """Close the issue, with ``body`` as its one closing comment when given.

        `gh issue close --comment` is two things to the forge, and the first can succeed
        while the second fails. So a retry passes no body once :meth:`said` shows the
        comment is already there, and the issue is only closed.
        """
        args = ["issue", "close", url] + (["--comment", body] if body else [])
        code, _out, err = self._gh(args)
        if code != 0:
            log.warning("[deficiencies] gh could not close %s: %s", url, err.strip())
        return code == 0

    def said(self, url: str, marker: str) -> bool | None:
        """Whether a comment on ``url`` already carries ``marker``; ``None`` if unreadable.

        Unreadable is answered as "said" by every caller: a comment missed is better
        than one posted twice on every flush.
        """
        code, out, _err = self._gh(["issue", "view", url, "--json", "comments"])
        if code != 0:
            return None
        try:
            comments = json.loads(out).get("comments") or []
            return any(marker in str(c.get("body") or "") for c in comments)
        except (ValueError, AttributeError):
            return None


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


def _spec(kind: str) -> Kind:
    return KINDS.get(kind) or KINDS[UNHANDLED_EXCEPTION]


def _stale(deficiency: Deficiency, born: datetime, now: datetime) -> bool:
    """Whether this row's newest evidence is too old to open an issue about.

    Both things must be true, and a row is buried only when they are:

    - it has not happened for :data:`STALE_AFTER_SECONDS`, and
    - it last happened before this *version* first ran on this machine, so it was last
      seen under an older one.

    Either alone is not enough, and that is deliberate on both sides. A row still being
    recorded under this version is this version's problem however old the first
    occurrence is. A row recorded minutes before an upgrade is still news — the upgrade
    it crossed says nothing about whether the new version fixed it — so an upgrade never
    buries recent evidence. What it buries is the backlog: rows nobody has seen for two
    days, last seen under a version this machine has since replaced.
    """
    last = _moment(deficiency.last_seen)
    if last is None:
        return False
    return (now - last).total_seconds() > STALE_AFTER_SECONDS and last < born


def _quiet(deficiency: Deficiency, now: datetime) -> bool:
    last = _moment(deficiency.last_seen)
    return last is not None and (now - last).total_seconds() > QUIET_BEFORE_CLOSE_SECONDS


def _tried_recently(deficiency: Deficiency, now: datetime) -> bool:
    """Whether closing this issue was already tried within the backoff."""
    tried = _moment(deficiency.close_tried_at)
    return tried is not None and (now - tried).total_seconds() < CLOSE_RETRY_AFTER_SECONDS


def closing_marker(body: str) -> str:
    """The first line of a closing comment: what tells a retry that it was already said."""
    return body.splitlines()[0].strip() if body.strip() else ""


def issue_for_kind(conn: Any, kind: str) -> str | None:
    """The issue the runtime opened about ``kind``, if it has opened one."""
    row = conn.execute(
        "SELECT issue_url FROM deficiencies WHERE kind = ? AND issue_url IS NOT NULL "
        "AND status = ? ORDER BY opened_at LIMIT 1",
        (kind, REPORTED),
    ).fetchone()
    return str(row["issue_url"]) if row is not None and row["issue_url"] else None


def quiet_body(deficiency: Deficiency) -> str:
    """The one comment an issue gets as the runtime closes it for having stopped."""
    days = int(QUIET_BEFORE_CLOSE_SECONDS // 86400)
    return _body_text(
        f"closing: not seen since {deficiency.last_seen}.\n\n"
        f"The runtime has recorded no occurrence of this for {days} days, across at least "
        "one build it had not run when this was last seen, so whatever caused it is gone. "
        "It reopens itself with the new evidence if it happens again; nothing here needs "
        "a person."
    )


def superseded_body(kind: str, issue: str | None) -> str:
    """The one comment an issue of a retired kind gets as it is closed."""
    spec = KINDS.get(kind)
    where = f" Its report is {issue}." if issue else " It has not been reported yet."
    return _body_text(
        f"superseded by `{kind}`; closing.\n\n"
        f"The runtime no longer records this kind. {spec.title if spec else kind} is what it "
        f"records instead, which counts one episode per subject rather than one per "
        f"repetition.{where}"
    )


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


# ── which build was running, and for how long ───────────────────────────────


#: The released part of a version: `0.1.22` out of `0.1.22-3-gabc1234.dirty`.
_RELEASED = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def released_version(described: str) -> str:
    """The released part of a `git describe`: ``0.1.22`` out of ``0.1.22-3-gabc1234.dirty``.

    `papaya_agent_runtime.__version__` is `git describe`, so it carries the commits since
    the tag and a dirty flag, and a ledger keyed on that would call every commit and
    every uncommitted edit a new version — burying every waiting deficiency at each
    restart, and reading as "a newer version has run" when nothing was released. A
    version with no release in it at all is ``unknown``: one version for ever, which
    buries nothing and closes nothing.
    """
    found = _RELEASED.search(str(described or ""))
    return found.group(0) if found is not None else "unknown"


def build_id() -> str:
    """Which released version of the runtime is running, as the ledger tells them apart."""
    try:
        from papaya_agent_runtime import __version__

        return released_version(str(__version__))
    except Exception:  # noqa: BLE001 - a version that cannot be read is one build
        return "unknown"


def _moment(stamp: str | None) -> datetime | None:
    """An ISO stamp from the ledger as an aware ``datetime``, or ``None``."""
    try:
        at = datetime.fromisoformat(str(stamp or ""))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def build_started(conn: Any, build: str, born: datetime) -> datetime:
    """When this machine first ran ``build``, recording ``born`` the first time.

    Not the moment this is called: a deficiency recorded seconds before the first
    flush of a new build belongs to it, so the build's life starts when the process
    that reports it did. The first reporter on a build wins, so a deficiency recorded
    on this build by an earlier process that never flushed is held back until its next
    occurrence — which errs the way the rest of this module does, towards saying less.
    """
    conn.execute(
        "INSERT OR IGNORE INTO runtime_builds (build_id, first_seen) VALUES (?, ?)",
        (build, born.isoformat(timespec="seconds")),
    )
    conn.commit()
    row = conn.execute(
        "SELECT first_seen FROM runtime_builds WHERE build_id = ?", (build,)
    ).fetchone()
    return (_moment(row["first_seen"]) if row is not None else None) or born


def newer_build_since(conn: Any, stamp: str | None) -> bool:
    """Whether a build of this runtime was first seen after ``stamp``."""
    at = _moment(stamp)
    if at is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM runtime_builds WHERE first_seen > ? LIMIT 1",
        (at.isoformat(timespec="seconds"),),
    ).fetchone()
    return row is not None


# ── opening issues ──────────────────────────────────────────────────────────


class Reporter:
    """Turns the ledger into issues on the runtime's own repository. `serve` runs one.

    Every collaborator with an outside world is a seam: ``forge`` (GitHub, a
    :class:`GhForge` by default), ``clock`` (an aware ``datetime``; the daily cap is
    counted on its UTC date), ``config`` (a :class:`Settings`, read from
    ``self_report.*`` when absent), ``origin`` (the checkout's origin URL) and
    ``build`` (which version of the runtime is running).

    A reporter's own start is the running version's start of life when the ledger has
    never seen that version before, which is what :func:`_stale` reads.
    """

    def __init__(
        self,
        *,
        forge: Any = None,
        clock: Callable[[], datetime] | None = None,
        config: Settings | Callable[[], Settings] | None = None,
        origin: Callable[[], str | None] | None = None,
        build: Callable[[], str] | None = None,
    ) -> None:
        self._forge = forge or GhForge()
        self._clock = clock or _now
        self._config = config
        self._origin = origin
        self._build = build or build_id
        self._born = self._clock()
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
        now = self._clock().astimezone(UTC)
        today = now.date().isoformat()
        conn = init_db()
        try:
            born = build_started(conn, self._build(), self._born)
            rows = [Deficiency.from_row(r) for r in conn.execute("SELECT * FROM deficiencies")]
            opened_today = sum(1 for d in rows if (d.opened_at or "").startswith(today))
            closed_today = sum(1 for d in rows if (d.closed_at or "").startswith(today))
            # Retiring a kind comes first: a row of one must get its closing comment, not
            # a recurrence comment this flush and a closing one on the next.
            done += self._close_what_is_over(conn, rows, config, now, closed_today)
            rows = [Deficiency.from_row(r) for r in conn.execute("SELECT * FROM deficiencies")]
            pending = sorted((d for d in rows if d.status == PENDING), key=lambda d: d.first_seen)
            for deficiency in pending:
                if _spec(deficiency.kind).superseded_by:
                    continue  # a retired kind opens nothing; the pass below closes its issue
                if _stale(deficiency, born, now):
                    conn.execute(
                        "UPDATE deficiencies SET status = ? WHERE fingerprint = ?",
                        (STALE, deficiency.fingerprint),
                    )
                    conn.commit()
                    log.info(
                        "[deficiencies] %s last happened at %s, before this build: not opening "
                        "an issue unless it happens again",
                        deficiency.fingerprint,
                        deficiency.last_seen,
                    )
                    continue
                if opened_today >= config.max_per_day:
                    continue
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
                    "UPDATE deficiencies SET reported_count = ?, status = ?, closed_at = NULL, "
                    "close_tried_at = NULL WHERE fingerprint = ?",
                    (deficiency.count, REPORTED, deficiency.fingerprint),
                )
                conn.commit()
                done.append(("reopened " if closed else "commented on ") + deficiency.issue_url)
        finally:
            conn.close()
        for line in done:
            log.info("[deficiencies] %s", line)
        return done

    def _close_what_is_over(
        self,
        conn: Any,
        rows: list[Deficiency],
        config: Settings,
        now: datetime,
        closed_today: int,
    ) -> list[str]:
        """Close the issues whose deficiency is over, and retire the kinds that are.

        Two ways an open issue ends without a person. A row of a kind another kind has
        replaced gets one comment naming the successor (and its issue, when it has one).
        A row nobody has seen for :data:`QUIET_BEFORE_CLOSE_SECONDS`, across at least one
        version this machine had not run when it was last seen, gets one comment saying
        so. Both are said once: the row carries ``closed_at`` afterwards, and a
        recurrence clears it and reopens the issue through the recurrence path.

        A forge that refuses must cost no more than a forge that agrees, so every
        *attempt* counts against the same ``max_per_day`` as opening, and a row that was
        tried within :data:`CLOSE_RETRY_AFTER_SECONDS` is left alone. Closing is two
        things to GitHub — a comment and a close — and the first can succeed while the
        second fails; so before commenting again a retry asks whether the comment is
        already there and, if it is (or cannot be read), only closes.
        """
        done: list[str] = []
        for deficiency in rows:
            if deficiency.status in (WATCHING, RECLASSIFIED, SUPERSEDED) or deficiency.closed_at:
                continue
            successor = _spec(deficiency.kind).superseded_by
            reported = deficiency.status == REPORTED and bool(deficiency.issue_url)
            if successor and not reported:
                # Retired, and never worth an issue: it leaves the ledger quietly.
                conn.execute(
                    "UPDATE deficiencies SET status = ? WHERE fingerprint = ?",
                    (SUPERSEDED, deficiency.fingerprint),
                )
                conn.commit()
                continue
            if not reported or deficiency.count > deficiency.reported_count:
                continue  # never opened, or its recurrence was just commented: not over
            if successor:
                body = superseded_body(successor, issue_for_kind(conn, successor))
                status, said = SUPERSEDED, f"closed {deficiency.issue_url} as superseded"
            elif _quiet(deficiency, now) and newer_build_since(conn, deficiency.last_seen):
                body = quiet_body(deficiency)
                status, said = REPORTED, f"closed {deficiency.issue_url}: not seen since "
                said += str(deficiency.last_seen)
            else:
                continue
            if _tried_recently(deficiency, now) or closed_today >= config.max_per_day:
                continue
            closed_today += 1
            conn.execute(
                "UPDATE deficiencies SET close_tried_at = ? WHERE fingerprint = ?",
                (_stamp(self._clock), deficiency.fingerprint),
            )
            conn.commit()
            if not self._end_issue(str(deficiency.issue_url), body):
                continue
            conn.execute(
                "UPDATE deficiencies SET status = ?, closed_at = ? WHERE fingerprint = ?",
                (status, _stamp(self._clock), deficiency.fingerprint),
            )
            conn.commit()
            done.append(said)
        return done

    def _end_issue(self, url: str, body: str) -> bool:
        """Close ``url`` with ``body`` as its one closing comment. Safe to call again.

        An issue already closed needs nothing. An issue whose state cannot be read is
        left for the next attempt rather than guessed at.
        """
        state = self._forge.state(url) or ""
        if state == "CLOSED":
            return True
        if not state:
            return False
        said = self._forge.said(url, closing_marker(body))
        return bool(self._forge.close(url, "" if said is not False else body))

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

        `serve` runs this at start, and it is how a change to :func:`reduce_turn_report`
        reaches rows that were written under the rule before it: two wordings of one
        cause that used to be two rows and two issues become one. Each group that
        reduces to one cause today keeps the row with the earliest issue; every other
        open issue in the group gets one comment, "duplicate of #N", and is closed, and
        its occurrences join the kept row. A duplicate whose issue cannot be closed now
        stays as it is and is tried again at the next start.

        A kept row keeps the fingerprint its issue was opened under, whatever today's
        rule would give it, and every other fingerprint in the group — including the one
        today's rule computes — becomes an alias to it. So the same line said again
        comments that issue instead of opening a second one, even while a duplicate's
        own issue is still waiting to be closed.
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
            at = _stamp(self._clock)
            for key, group in groups.items():
                group.sort(key=_merge_order)
                kept, rest = group[0], group[1:]
                if not rest and canonical_fingerprint(conn, key) == kept.fingerprint:
                    continue  # nothing to fold and the alias is already right
                # Before anything the forge can refuse: what this cause is called today
                # points at the row that already has the issue for it.
                remember_alias(conn, key, kept.fingerprint, at)
                conn.commit()
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
                if merged:
                    _fold(conn, kept, merged, at)
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
        "The runtime now fingerprints a turn's `RUNTIME:` line by its cause (the pull "
        "request or issue it names; else the exception and where it came from; else the "
        "tool and the refusal) rather than its wording, and this report has the same "
        "cause as that one. Its occurrences are counted there."
    )


def _fold(conn: Any, kept: Deficiency, merged: list[Deficiency], at: str) -> None:
    """Give ``kept`` ``merged``'s occurrences, drop their rows, and alias their keys to it."""
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
    for duplicate in merged:
        remember_alias(conn, duplicate.fingerprint, kept.fingerprint, at)
    conn.execute(
        "INSERT INTO deficiencies (fingerprint, kind, title, detail, first_seen, last_seen, "
        "count, evidence, issue_url, status, opened_at, reported_count, closed_at, "
        "close_tried_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kept.fingerprint,
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
            kept.closed_at,
            kept.close_tried_at,
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
            "SELECT evidence FROM deficiencies WHERE fingerprint = ?",
            (canonical_fingerprint(conn, fingerprint(kind, detail)),),
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
        # Every key this row can be under: as written before reduced fingerprints, as
        # task 285 reduced it, and as task 321 reads it (the line names pull request
        # #710, which leads unless the line also names where it broke).
        fingerprints=frozenset({"73e968fea9ac0d8e", "c4b7a8dab0a58df5", "400aea7b2154f11e"}),
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
    "LIFELINE_DOWN",
    "FIXED_CHECKINS",
    "FIXED_TURN_REPORTS",
    "GATE_PAST_TOOL_CAP",
    "KINDS",
    "LABEL",
    "MISSED_TURN",
    "PENDING",
    "PROMPT_CLARITY",
    "PROMPT_DEFECT",
    "QUIET_BEFORE_CLOSE_SECONDS",
    "READINESS_UNREMEDIED",
    "RECLASSIFIED",
    "REPEATED_STEER",
    "REPEATED_WITHOUT_PROGRESS",
    "REPORTED",
    "RUNTIME_CI_RED",
    "STALE",
    "STALE_AFTER_SECONDS",
    "STALL_WHILE_LIVE",
    "SUPERSEDED",
    "TURN_REPORT",
    "UNHANDLED_EXCEPTION",
    "WATCHING",
    "WORKER_DENIAL",
    "CLOSE_RETRY_AFTER_SECONDS",
    "CheckinFix",
    "Deficiency",
    "GhForge",
    "Reporter",
    "Settings",
    "add_listener",
    "build_id",
    "build_started",
    "canonical_fingerprint",
    "closing_marker",
    "comment_body",
    "denial_kinds",
    "duplicate_body",
    "fingerprint",
    "issue_for_kind",
    "fixed_checkin",
    "github_slug",
    "issue_body",
    "issue_number",
    "ledger",
    "newer_build_since",
    "normalise",
    "notify",
    "quiet_body",
    "record",
    "record_denials",
    "record_exception",
    "record_gate_past_tool_cap",
    "record_once",
    "reclassified_body",
    "released_version",
    "redact",
    "reduce_turn_report",
    "remember_alias",
    "remove_listener",
    "runtime_repo",
    "settings",
    "summary",
    "superseded_body",
]
