"""What this machine needs from its owner, and making sure it reaches them.

Readiness answers "can this runtime take work?". Some of its answers are the
runtime's own to close (a missing config, task 267's drifted defaults); the rest
need a person at this machine — `gh` signed out, Docker stopped, the disk full. The
second kind is a *blocker*, and a blocker nobody hears about is the failure this
module exists for: delivery used to fail on the first pull request with a gh error
nobody saw.

So every readiness problem that carries ``steps`` becomes an entry in a small
ledger (`.ppy/blockers.json`): code, title, steps, first and last seen. The ledger
decides when a person is told:

- when a blocker **appears**;
- again only when its **steps change** (a new device code, a different fix) or it
  has stood for :data:`REPEAT_AFTER`;
- and **once** when it clears — only if it was ever reported, since "fixed" is not
  news to somebody who never heard it was broken.

It reaches them three ways, all private to them: the agent's DM with the person who
connected this machine (`serve`), the supervised protocol's `runtime.blockers` on
`hello` and every `status` (the desktop app's "Setup needed on this Mac" card), and
— when a job is refused because of one — a single neutral comment on the ticket,
:data:`TICKET_COMMENT`, that never names the blocker.

Nothing private leaves in any of them. A blocker's public form is its code, a title,
the steps and when it was first seen; every string passes through :func:`redact`
(tokens, home paths, email addresses and diff hunks), and the DM names the machine
only by its short hostname.

A JSON file rather than a table: it is machine-local, tiny, rewritten whole, and
read by the protocol writer on every `status` without a database connection.

GitHub's device flow (:class:`DeviceFlow`) closes the most common blocker without a
terminal when Papaya provides an OAuth app (`forge.github_oauth_client_id`): the
code goes to the owner through the same three ways, the token goes straight into
`gh`'s own store on stdin and nowhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from papaya_agent_runtime import readiness

log = logging.getLogger("papaya_agent_runtime.blockers")

#: How long a blocker may stand before its owner is reminded of it.
REPEAT_AFTER = timedelta(hours=24)

#: The one comment a ticket gets when this machine refuses it for a blocker. It
#: names nothing: not the blocker, not the machine, not a command.
TICKET_COMMENT = (
    "This machine needs setup before it can take this; its owner has been told what to do."
)

#: The `sweep` decline reason for a ticket refused because of a blocker.
DECLINE_REASON = "this machine needs setup before it can take work"

#: How long after a device-flow attempt ends before another one is started.
DEVICE_RETRY = timedelta(minutes=30)

#: The sweep's blocker: Papaya refuses this machine work nobody is doing.
IDLE_WORK_KEPT = "papaya_keeps_idle_work"

#: Blocker codes a readiness verdict never carries, so observing one never clears them.
OBSERVED_ELSEWHERE = frozenset({IDLE_WORK_KEPT})


# ── redaction ────────────────────────────────────────────────────────────────

_TOKEN = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|pagc_[A-Za-z0-9_\-]+"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]*"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=\-]+"
    r"|(?i:(?:token|password|secret)\s*[=:]\s*)\S+"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_HOME = re.compile(r"(?:/Users|/home)/[^/\s:'\"`]+|[A-Za-z]:\\Users\\[^\\\s]+")
_DIFF_START = re.compile(r"^(?:diff --git |@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@|--- a/|\+\+\+ b/)")
_DIFF_BODY = re.compile(r"^(?:[ +\-\\]|index [0-9a-f]+\.\.|--- |\+\+\+ |@@ )")


def redact(text: str) -> str:
    """``text`` with tokens, home paths, email addresses and diff hunks taken out.

    Blocker steps are written by this runtime from templates and should never
    carry any of these; this is the backstop that makes "should" a guarantee on
    every surface that leaves the machine.
    """
    lines: list[str] = []
    in_diff = False
    for line in str(text).splitlines() or [""]:
        if _DIFF_START.match(line) or (in_diff and _DIFF_BODY.match(line)):
            if not in_diff:
                lines.append("[diff removed]")
            in_diff = True
            continue
        in_diff = False
        lines.append(line)
    out = "\n".join(lines)
    out = _TOKEN.sub("[redacted]", out)
    home = str(Path.home())
    if home and home != "/":
        out = out.replace(home, "~")
    out = _HOME.sub("~", out)
    return _EMAIL.sub("[email]", out)


def short_hostname() -> str:
    """This machine's name without its domain, which is all a report says about it."""
    try:
        return socket.gethostname().split(".", 1)[0].strip() or "this machine"
    except OSError:
        return "this machine"


# ── the ledger ──────────────────────────────────────────────────────────────


def ledger_path() -> Path:
    from papaya_agent_runtime.paths import ppy_home

    return ppy_home() / "blockers.json"


def fingerprint(problem: readiness.Problem) -> str:
    """One blocker's identity: its code and scope, never its wording or its repos."""
    key = f"{problem.code}:{problem.scope}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class Blocker:
    """One thing a person has to do on this machine, and what they have been told."""

    fingerprint: str
    code: str
    title: str
    steps: list[str]
    first_seen: str
    last_seen: str
    #: Internal only: what it is about and whose work it stops. Never published.
    scope: str = ""
    repos: list[str] = field(default_factory=list)
    reported_at: str | None = None
    reported_steps: list[str] | None = None
    cleared_at: str | None = None

    def public(self) -> dict[str, Any]:
        """The shape every surface carries: `{code, title, steps, since}`, redacted."""
        return {
            "code": self.code,
            "title": redact(self.title),
            "steps": [redact(step) for step in self.steps],
            "since": self.first_seen,
        }


@dataclass
class Changes:
    """What one observation did to the ledger."""

    appeared: list[Blocker] = field(default_factory=list)
    changed: list[Blocker] = field(default_factory=list)
    cleared: list[Blocker] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.appeared or self.changed or self.cleared)


_lock = threading.Lock()


class Ledger:
    """`.ppy/blockers.json`: open blockers, cleared ones not yet reported, commented tickets."""

    def __init__(
        self,
        open_: dict[str, Blocker] | None = None,
        cleared: dict[str, Blocker] | None = None,
        commented: dict[str, list[str]] | None = None,
    ) -> None:
        self.open: dict[str, Blocker] = open_ or {}
        self.cleared: dict[str, Blocker] = cleared or {}
        #: Work item id -> the blockers it was commented on for, so a ticket offered
        #: again while the same blocker stands does not get a second comment.
        self.commented: dict[str, list[str]] = commented or {}

    @classmethod
    def load(cls) -> Ledger:
        try:
            data = json.loads(ledger_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()

        def blockers(key: str) -> dict[str, Blocker]:
            found: dict[str, Blocker] = {}
            for fp, raw in (data.get(key) or {}).items():
                with contextlib.suppress(TypeError):
                    found[str(fp)] = Blocker(**raw)
            return found

        commented = data.get("commented") or {}
        return cls(
            blockers("open"),
            blockers("cleared"),
            {str(k): [str(fp) for fp in v] for k, v in commented.items() if isinstance(v, list)},
        )

    def save(self) -> None:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "open": {fp: asdict(b) for fp, b in sorted(self.open.items())},
            "cleared": {fp: asdict(b) for fp, b in sorted(self.cleared.items())},
            "commented": self.commented,
        }
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)

    # -- what readiness found ------------------------------------------------

    def observe(
        self,
        verdict: readiness.Readiness,
        now: datetime,
        overrides: dict[str, list[str]] | None = None,
    ) -> Changes:
        """Bring the ledger in line with a verdict. ``overrides`` replace a blocker's steps."""
        overrides = overrides or {}
        changes = Changes()
        seen: set[str] = set()
        stamp = _iso(now)
        for problem in verdict.problems:
            if not problem.steps:
                continue
            fp = fingerprint(problem)
            if fp in seen:
                continue
            seen.add(fp)
            steps = list(overrides.get(fp) or problem.steps)
            existing = self.open.get(fp)
            if existing is None:
                blocker = Blocker(
                    fingerprint=fp,
                    code=problem.code,
                    title=problem.title or problem.summary,
                    steps=steps,
                    first_seen=stamp,
                    last_seen=stamp,
                    scope=problem.scope,
                    repos=list(problem.repos),
                )
                self.open[fp] = blocker
                # A blocker that cleared and came back before the clearing was said
                # is the same blocker still: nobody needs to hear either change.
                self.cleared.pop(fp, None)
                changes.appeared.append(blocker)
                continue
            if existing.steps != steps or existing.title != (problem.title or problem.summary):
                changes.changed.append(existing)
            existing.steps = steps
            existing.title = problem.title or problem.summary
            existing.repos = list(problem.repos)
            existing.last_seen = stamp
        for fp in [fp for fp in self.open if fp not in seen]:
            if self.open[fp].code in OBSERVED_ELSEWHERE:
                continue  # not readiness's to clear
            blocker = self.open.pop(fp)
            blocker.cleared_at = stamp
            if blocker.reported_at is not None:
                self.cleared[fp] = blocker
            changes.cleared.append(blocker)
        return changes

    # -- what the owner has been told --------------------------------------

    def due(self, now: datetime) -> tuple[list[Blocker], list[Blocker]]:
        """The open blockers to report now, and the cleared ones to report once."""
        opened = []
        for blocker in self.open.values():
            reported = _parse(blocker.reported_at)
            if (
                reported is None
                or blocker.reported_steps != blocker.steps
                or now - reported >= REPEAT_AFTER
            ):
                opened.append(blocker)
        return (
            sorted(opened, key=lambda b: (b.first_seen, b.code)),
            sorted(self.cleared.values(), key=lambda b: (b.cleared_at or "", b.code)),
        )

    def mark_reported(self, opened: list[Blocker], cleared: list[Blocker], now: datetime) -> None:
        for blocker in opened:
            if blocker.fingerprint in self.open:
                self.open[blocker.fingerprint].reported_at = _iso(now)
                self.open[blocker.fingerprint].reported_steps = list(blocker.steps)
        for blocker in cleared:
            self.cleared.pop(blocker.fingerprint, None)

    def public(self) -> list[dict[str, Any]]:
        return [
            b.public() for b in sorted(self.open.values(), key=lambda b: (b.first_seen, b.code))
        ]

    # -- tickets refused because of one -------------------------------------

    def should_comment(self, work_item_id: str, fingerprints: list[str]) -> bool:
        """Record a comment on this ticket, unless it already has one for these blockers."""
        already = set(self.commented.get(str(work_item_id), []))
        if set(fingerprints) <= already:
            return False
        self.commented[str(work_item_id)] = sorted(already | set(fingerprints))
        return True

    def release_comments(self, fingerprints: set[str]) -> list[str]:
        """Forget the comments made for blockers that cleared; the tickets they were on."""
        freed = []
        for item, fps in list(self.commented.items()):
            left = [fp for fp in fps if fp not in fingerprints]
            if len(left) != len(fps):
                freed.append(item)
            if left:
                self.commented[item] = left
            else:
                del self.commented[item]
        return freed


def update(
    verdict: readiness.Readiness,
    *,
    now: datetime | None = None,
    overrides: dict[str, list[str]] | None = None,
) -> Changes:
    """Observe ``verdict`` into the ledger on disk. Tickets freed by a clearing are re-offered."""
    moment = now or datetime.now(UTC)
    with _lock:
        ledger = Ledger.load()
        changes = ledger.observe(verdict, moment, overrides)
        freed = ledger.release_comments({b.fingerprint for b in changes.cleared})
        ledger.save()
    if freed:
        from papaya_agent_runtime import sweep

        for item in freed:
            # Declined while this machine could not work; now it can, so the sweep
            # may offer it again if nobody else has taken it meanwhile.
            with contextlib.suppress(Exception):
                sweep.forget_declined(item)
    return changes


def idle_work_kept(names: list[str]) -> str:
    """The sentence a person reads about idle work Papaya will not let this machine take."""
    return (
        f"Papaya keeps {len(names)} idle item{'' if len(names) == 1 else 's'} from this Mac: "
        f"{', '.join(names)}; use Run on this Mac, or wait for the guard to lift"
    )


def set_idle_work_kept(names: list[str], *, now: datetime | None = None) -> Changes:
    """Keep the sweep's one blocker in line with the idle items Papaya refused this sweep.

    It appears with the first refused item, changes (and is said again) when the set
    changes, and clears when the set is empty. Readiness rounds leave it alone.
    """
    stamp = _iso(now or datetime.now(UTC))
    fp = hashlib.sha256(f"{IDLE_WORK_KEPT}:".encode()).hexdigest()[:16]
    names = sorted(dict.fromkeys(str(name) for name in names if name))
    changes = Changes()
    with _lock:
        ledger = Ledger.load()
        existing = ledger.open.get(fp)
        if not names:
            if existing is None:
                return changes
            blocker = ledger.open.pop(fp)
            blocker.cleared_at = stamp
            if blocker.reported_at is not None:
                ledger.cleared[fp] = blocker
            changes.cleared.append(blocker)
        else:
            title = idle_work_kept(names)
            steps = [
                f"In Papaya, open {', '.join(names)} and use Run on this Mac to send "
                f"{'it' if len(names) == 1 else 'them'} here",
                "Or wait: the sweep asks again every round and takes the work once "
                "Papaya's guard lifts",
            ]
            if existing is None:
                blocker = Blocker(fp, IDLE_WORK_KEPT, title, steps, stamp, stamp)
                ledger.open[fp] = blocker
                ledger.cleared.pop(fp, None)
                changes.appeared.append(blocker)
            else:
                if existing.title != title:
                    existing.title, existing.steps = title, steps
                    changes.changed.append(existing)
                existing.last_seen = stamp
        ledger.save()
    return changes


def current() -> list[dict[str, Any]]:
    """The open blockers in their public shape: what `runtime.blockers` carries."""
    try:
        return Ledger.load().public()
    except Exception:  # noqa: BLE001 - a protocol message must never fail on this
        return []


def comment_on(work_item_id: str | None, verdict: readiness.Readiness) -> bool:
    """Whether a refused ticket should get :data:`TICKET_COMMENT` now (and record it)."""
    if not work_item_id:
        return False
    fingerprints = [fingerprint(p) for p in verdict.problems if p.steps]
    with _lock:
        ledger = Ledger.load()
        if not ledger.should_comment(str(work_item_id), fingerprints):
            return False
        ledger.save()
    return True


def from_verdict(verdict: readiness.Readiness) -> list[dict[str, Any]]:
    """The blockers in a verdict, public shape, with `since` from the ledger when known."""
    ledger = Ledger.load()
    now = _iso(datetime.now(UTC))
    out = []
    for problem in verdict.problems:
        if not problem.steps:
            continue
        known = ledger.open.get(fingerprint(problem))
        out.append(
            Blocker(
                fingerprint=fingerprint(problem),
                code=problem.code,
                title=problem.title or problem.summary,
                steps=list(known.steps if known else problem.steps),
                first_seen=known.first_seen if known else now,
                last_seen=now,
            ).public()
        )
    return out


# ── saying it ───────────────────────────────────────────────────────────────


def message(opened: list[Blocker], cleared: list[Blocker], *, host: str) -> str:
    """The owner's DM: what to do on which machine, and what is fixed now."""
    lines: list[str] = []
    machine = redact(host)
    if opened:
        lines.append(f"**Setup needed on {machine}.** Until it is done, work there waits.")
        for blocker in opened:
            public = blocker.public()
            lines.append("")
            lines.append(f"**{public['title']}**")
            for number, step in enumerate(public["steps"], start=1):
                lines.append(f"{number}. {step}")
    if cleared:
        if lines:
            lines.append("")
        for blocker in cleared:
            lines.append(f"Fixed on {machine}: {redact(blocker.title)}.")
    return "\n".join(lines)


def render_text(blockers: list[dict[str, Any]]) -> str:
    """`ppy blockers`, `ppy doctor` and `ppy serve`'s start: each blocker with its steps."""
    if not blockers:
        return "no blockers: nothing on this machine needs a person"
    lines = []
    for blocker in blockers:
        lines.append(f"{blocker['code']}: {blocker['title']} (since {blocker['since']})")
        for number, step in enumerate(blocker["steps"], start=1):
            lines.append(f"  {number}. {step}")
    return "\n".join(lines)


# ── GitHub's device flow ────────────────────────────────────────────────────


class DeviceFlowError(Exception):
    """The device flow ended without a token. The message never carries one."""


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: float
    expires_in: float


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https host
            data = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DeviceFlowError(f"GitHub could not be reached: {type(exc).__name__}") from None
    return data if isinstance(data, dict) else {}


class DeviceFlow:
    """GitHub's OAuth device flow, ending in `gh auth login --with-token`.

    ``http(url, fields) -> dict`` and ``run(argv, input=...) -> (status, output)``
    are the seams: a fake GitHub and a fake `gh`.
    """

    SCOPES = "repo read:org workflow"

    def __init__(
        self,
        client_id: str,
        host: str = "github.com",
        *,
        http: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
        run: Callable[..., tuple[int, str]] | None = None,
    ) -> None:
        self.client_id = client_id
        self.host = host
        self._http = http or _post_form
        self._run = run or readiness.machine.run

    def request_code(self) -> DeviceCode:
        data = self._http(
            f"https://{self.host}/login/device/code",
            {"client_id": self.client_id, "scope": self.SCOPES},
        )
        try:
            return DeviceCode(
                device_code=str(data["device_code"]),
                user_code=str(data["user_code"]),
                verification_uri=str(
                    data.get("verification_uri") or f"https://{self.host}/login/device"
                ),
                interval=float(data.get("interval") or 5),
                expires_in=float(data.get("expires_in") or 900),
            )
        except (KeyError, TypeError, ValueError):
            raise DeviceFlowError(
                f"GitHub did not issue a device code: {data.get('error')}"
            ) from None

    async def wait(
        self,
        code: DeviceCode,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> str:
        """Poll until the person authorises, and return the token (to hand straight to gh)."""
        interval = max(code.interval, 1.0)
        deadline = clock() + code.expires_in
        while clock() < deadline:
            await sleep(interval)
            data = await asyncio.to_thread(
                self._http,
                f"https://{self.host}/login/oauth/access_token",
                {
                    "client_id": self.client_id,
                    "device_code": code.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            token = data.get("access_token")
            if token:
                return str(token)
            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval = float(data.get("interval") or interval + 5)
                continue
            raise DeviceFlowError(f"GitHub ended the sign-in: {error or 'no token'}")
        raise DeviceFlowError("GitHub ended the sign-in: expired_token")

    def install(self, token: str) -> None:
        """Give the token to gh on stdin. It is not logged, stored or echoed anywhere else."""
        status, _ = self._run(
            ["gh", "auth", "login", "--hostname", self.host, "--with-token"], input=token
        )
        if status != 0:
            raise DeviceFlowError(f"gh did not accept the sign-in (exit {status})")
        self._run(["gh", "auth", "setup-git", "--hostname", self.host])

    def steps(self, code: DeviceCode) -> list[str]:
        return [
            f"open {code.verification_uri}",
            f"enter the code {code.user_code}",
            "approve access for this machine's GitHub CLI",
            "Nothing else: the runtime installs the sign-in into gh itself and starts "
            "taking work within a few minutes.",
        ]


def github_client_id() -> str:
    """`forge.github_oauth_client_id`, or empty when unset or unreadable."""
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        return str(load_config().forge.github_oauth_client_id or "").strip()
    except (ConfigError, OSError, AttributeError):
        return ""


# ── the watch `serve` runs ──────────────────────────────────────────────────


class Watch:
    """Re-run readiness on a timer, keep the ledger, tell the owner, drive the device flow.

    Every collaborator is a seam: ``check`` (readiness), ``say(text) -> bool`` (the
    DM; ``True`` when it landed), ``publish()`` (a `status` to a supervised host),
    ``sleep``, ``clock`` (an aware datetime), ``client_id()`` and ``device_flow``
    (``(client_id, host) -> DeviceFlow``).
    """

    def __init__(
        self,
        *,
        check: Callable[[], readiness.Readiness] | None = None,
        say: Callable[[str], Awaitable[bool]] | None = None,
        publish: Callable[[], None] | None = None,
        interval: float = 300.0,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        client_id: Callable[[], str] | None = None,
        device_flow: Callable[[str, str], DeviceFlow] | None = None,
        host: str | None = None,
    ) -> None:
        self._check = check or readiness.check
        self._say = say
        self._publish = publish
        self._interval = float(interval)
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client_id = client_id or github_client_id
        self._device_flow = device_flow or DeviceFlow
        self.host = host or short_hostname()
        self._lock = asyncio.Lock()
        #: Fingerprint -> the device-flow steps standing in for the manual ones.
        self._overrides: dict[str, list[str]] = {}
        self._flows: dict[str, asyncio.Task] = {}
        self._flow_ended: dict[str, datetime] = {}

    async def round(
        self,
        *,
        verdict: readiness.Readiness | None = None,
        lead: str = "",
        on_said: Callable[[], None] | None = None,
    ) -> readiness.Readiness:
        """Check, record, start a sign-in if one can close a blocker, and say what is new.

        ``verdict`` skips the check when the caller has just made one. ``lead`` is
        put before the blockers in the same DM (the start-up readiness report rides
        along), and ``on_said`` runs only when that DM landed.
        """
        async with self._lock:
            if verdict is None:
                verdict = await asyncio.to_thread(self._check)
            await self._start_sign_ins(verdict)
            changes = await asyncio.to_thread(
                update, verdict, now=self._clock(), overrides=dict(self._overrides)
            )
            if await self._report(lead) and on_said is not None:
                on_said()
            if changes and self._publish is not None:
                with contextlib.suppress(Exception):
                    self._publish()
            return verdict

    async def settle(self) -> None:
        """Wait for every sign-in, and the round each one ends with, to finish. For tests."""
        while pending := [t for t in self._flows.values() if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        while True:
            await self._sleep(self._interval)
            try:
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the watch must outlive one bad round
                log.warning("[blockers] A readiness round failed: %s", exc)

    async def _report(self, lead: str) -> bool:
        """Say what is due; whether anything was said."""
        if self._say is None:
            return False
        now = self._clock()
        with _lock:
            ledger = Ledger.load()
        opened, cleared = ledger.due(now)
        body = message(opened, cleared, host=self.host)
        text = "\n\n".join(part for part in (lead, body) if part)
        if not text or not await self._say(redact(text)):
            return False
        with _lock:
            ledger = Ledger.load()
            ledger.mark_reported(opened, cleared, now)
            ledger.save()
        return True

    async def _start_sign_ins(self, verdict: readiness.Readiness) -> None:
        """Ask GitHub for a device code for each signed-out host that has no sign-in going.

        The code is asked for before the round records and reports anything, so the
        owner's first message already says "enter this code" rather than a manual
        sequence followed a moment later by a code.
        """
        client_id = self._client_id()
        if not client_id:
            return
        now = self._clock()
        for problem in verdict.problems:
            if problem.code != readiness.FORGE_UNAUTHENTICATED or not problem.scope:
                continue
            host = problem.scope
            running = self._flows.get(host)
            if running is not None and not running.done():
                continue
            ended = self._flow_ended.get(host)
            if ended is not None and now - ended < DEVICE_RETRY:
                continue
            flow = self._device_flow(client_id, host)
            try:
                code = await asyncio.to_thread(flow.request_code)
            except Exception as exc:  # noqa: BLE001 - no code leaves the manual steps standing
                log.warning("[blockers] Could not start a GitHub sign-in for %s: %s", host, exc)
                self._flow_ended[host] = now
                continue
            fp = fingerprint(problem)
            self._overrides[fp] = flow.steps(code)
            self._flows[host] = asyncio.create_task(self._sign_in(flow, code, fp))

    async def _sign_in(self, flow: DeviceFlow, code: DeviceCode, fp: str) -> None:
        signed_in = False
        try:
            token = await flow.wait(code, sleep=self._sleep)
            await asyncio.to_thread(flow.install, token)
            signed_in = True
            log.info("[blockers] Signed gh in to %s through the device flow", flow.host)
        except asyncio.CancelledError:
            raise
        except DeviceFlowError as exc:
            log.warning("[blockers] GitHub sign-in for %s did not finish: %s", flow.host, exc)
        except Exception as exc:  # noqa: BLE001 - a failed sign-in leaves the manual steps
            log.warning(
                "[blockers] GitHub sign-in for %s failed: %s", flow.host, type(exc).__name__
            )
        finally:
            self._overrides.pop(fp, None)
            self._flow_ended[flow.host] = self._clock()
        if signed_in:
            await self.round()

    async def close(self) -> None:
        tasks = list(self._flows.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


__all__ = [
    "DECLINE_REASON",
    "IDLE_WORK_KEPT",
    "OBSERVED_ELSEWHERE",
    "REPEAT_AFTER",
    "TICKET_COMMENT",
    "Blocker",
    "Changes",
    "DeviceCode",
    "DeviceFlow",
    "DeviceFlowError",
    "Ledger",
    "Watch",
    "comment_on",
    "current",
    "fingerprint",
    "from_verdict",
    "github_client_id",
    "idle_work_kept",
    "set_idle_work_kept",
    "message",
    "redact",
    "render_text",
    "short_hostname",
    "update",
]
