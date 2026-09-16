"""What this runtime is, said by the runtime itself.

The Papaya client detects a runtime checkout when somebody connects a machine, and
then has to decide what that checkout can do. It used to answer that by reading the
runtime's own `cli.py` and looking for a subcommand name. That is a guess dressed up
as a fact, and it broke silently the day the subcommand it looked for was removed:
nothing raised, the connection still looked healthy, and the client simply believed
the wrong thing about the machine in front of it.

So the runtime says what it is, on one command it owns. `ppy capabilities --json`
is the only thing anybody should ever read to learn this, and the shape it prints is
a contract:

    {"runtime": "papaya-agent-runtime", "version": "0.0.0",
     "client_version": "0.14.0", "protocol": 1, "modes": []}

Two properties make it usable at connect time, and both are constraints on what may
ever be added here. It reads **local state only** — no network, no harness probe, no
state database — so it answers instantly on a machine that is offline, unconfigured,
or both. And it **cannot fail**: an absent client is reported as `null` rather than
raised, because "I could not tell you" is a worse answer than "the client is not
installed here". Whether the runtime is *ready* is a different question with its own
command; see `readiness.py`.

`doctor`, `readiness` and — when it lands — `serve` read the client version from
here rather than each working it out, so there is one answer to compare against the
host's.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os

#: The name the client matches on. Fixed, never derived from anything.
RUNTIME = "papaya-agent-runtime"

#: What the supervised protocol is assumed to be when the client does not say.
#: Version 1 is the only one that has ever existed, so an older client that
#: declares nothing is a version-1 client rather than an unknown.
DEFAULT_PROTOCOL = 1

#: The launch modes this runtime can be started in — empty until `ppy serve`
#: exists. A client reading an empty list must not try to exec this checkout;
#: `supervised` and `terminal` arrive with the command that can serve them,
#: never before it, because a mode announced early is a promise the exec keeps.
MODES: tuple[str, ...] = ()

#: The version of the client that exec'd us, passed across the exec by the host.
HOST_CLIENT_VERSION_ENV = "PAPAYA_HOST_CLIENT_VERSION"

_CLIENT_MODULE = "papaya_agent_client"
_CLIENT_DIST = "papaya-agent-client"


def client_version() -> str | None:
    """The embedded client's version, or ``None`` if it cannot be imported.

    Importability is the question that matters, not whether a distribution is
    recorded somewhere: `ppy serve` will embed this package's loop in-process, so
    a client that installs but does not import is no client at all. That is why
    the import is attempted first and the metadata read only after it succeeds.
    """
    try:
        importlib.import_module(_CLIENT_MODULE)
    except Exception:  # noqa: BLE001 - any import failure means "not available here"
        return None
    try:
        return importlib.metadata.version(_CLIENT_DIST)
    except importlib.metadata.PackageNotFoundError:
        return None


def protocol() -> int:
    """The supervised-protocol version the embedded client speaks.

    Read from the client rather than restated here, so the two cannot drift: the
    client owns the protocol and this command only reports it. Anything
    unreadable — no client, no constant, a value that is not a number — falls
    back to version 1, which is what such a client would in fact be speaking.
    """
    try:
        client = importlib.import_module(_CLIENT_MODULE)
    except Exception:  # noqa: BLE001 - no client, so nothing to read a version from
        return DEFAULT_PROTOCOL
    declared = getattr(client, "PROTOCOL_VERSION", None)
    if declared is None:
        try:
            supervisor = importlib.import_module(f"{_CLIENT_MODULE}.supervisor")
        except Exception:  # noqa: BLE001 - the package is there, the seam is not
            return DEFAULT_PROTOCOL
        declared = getattr(supervisor, "PROTOCOL_VERSION", None)
    try:
        return int(declared)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_PROTOCOL


def collect() -> dict:
    """The capabilities object, exactly as `--json` prints it."""
    from papaya_agent_runtime import __version__

    return {
        "runtime": RUNTIME,
        "version": __version__,
        "client_version": client_version(),
        "protocol": protocol(),
        "modes": list(MODES),
    }


def render_text(data: dict) -> str:
    """The same fields, one per line, for a person rather than a client."""
    return "\n".join(
        [
            f"runtime: {data['runtime']}",
            f"version: {data['version']}",
            f"client_version: {data['client_version'] or '(not importable)'}",
            f"protocol: {data['protocol']}",
            f"modes: {', '.join(data['modes']) or '(none)'}",
        ]
    )


def client_line(data: dict | None = None) -> str:
    """One line naming the embedded client, for `doctor` and `readiness`.

    Both diagnostics answer "which client is in this checkout" identically, which
    is the point: when a host complains the runtime is behind, the number it is
    behind by has to be the same wherever the reader looks for it. Pass an
    already-collected object to report it rather than collect a second one.
    """
    caps = collect() if data is None else data
    version = caps["client_version"]
    if version is None:
        return (
            f"{_CLIENT_DIST} NOT IMPORTABLE — run `uv sync` (assuming protocol v{caps['protocol']})"
        )
    return f"{_CLIENT_DIST} {version} (supervised protocol v{caps['protocol']})"


# ── Are we behind the client that launched us? ──────────────────────────────


def host_client_version() -> str | None:
    """The client version the host exec'd us with, if it told us."""
    value = (os.environ.get(HOST_CLIENT_VERSION_ENV) or "").strip()
    return value or None


def _release(version: str) -> tuple[int, ...] | None:
    """The leading dotted-numeric release of a version, or ``None`` if unreadable.

    Deliberately small: a release comparison over `0.14.0` is all this needs, and
    the runtime has no version-parsing dependency to reach for. Anything with a
    suffix (`0.15.0rc1`, `0.15.0+local`) compares on its release segment, and
    anything that is not a release at all compares against nothing — an
    unparseable version must not be reported as newer, because a warning nobody
    can act on is worse than silence.
    """
    head = version.strip().split("+", 1)[0]
    parts: list[int] = []
    for piece in head.split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return None
        parts.append(int(digits))
        if digits != piece:  # a pre-release or dev suffix ends the release segment
            break
    return tuple(parts) or None


def client_behind_host() -> tuple[str, str] | None:
    """``(embedded, host)`` when the host's client is strictly newer, else ``None``.

    Strictly: an equal or older host is the normal case and says nothing. This is
    only ever a warning — a runtime that refused to work because its checkout was
    a patch release behind would be a worse failure than the drift it is naming.
    """
    host = host_client_version()
    if host is None:
        return None
    embedded = client_version()
    if embedded is None:
        return None
    host_release = _release(host)
    embedded_release = _release(embedded)
    if host_release is None or embedded_release is None:
        return None
    return (embedded, host) if host_release > embedded_release else None


__all__ = [
    "DEFAULT_PROTOCOL",
    "HOST_CLIENT_VERSION_ENV",
    "MODES",
    "RUNTIME",
    "client_behind_host",
    "client_line",
    "client_version",
    "collect",
    "host_client_version",
    "protocol",
    "render_text",
]
