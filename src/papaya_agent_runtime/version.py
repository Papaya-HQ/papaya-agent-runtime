"""What build this is, read from git, and what the next release should be called.

The runtime is only ever run from a checkout: there is no wheel, no `pip install`,
nothing that could stamp a version into the package as it was built. So the version
had to come from somewhere, and for a long time it came from a literal `0.0.0` in
two files — which is why Papaya Desktop's machine card said "Managed by Papaya Agent
Runtime 0.0.0" for every machine, on every build, forever.

**The tag is the version.** `main` requires a pull request and a green `ci-ok` with
no bypass actors, so no workflow can commit a version bump to it; a tag, on the other
hand, a workflow may push. `.github/workflows/release.yml` therefore tags every merge
to `main` with the next patch, and this module is the one place that knows both
halves of that arrangement:

- :func:`derive` reads the tag back out of the checkout, so `ppy version`,
  `ppy capabilities --json` and `papaya_agent_runtime.__version__` all report the
  build somebody is actually running;
- :func:`next_tag` computes what the next tag should be, and the workflow *calls*
  it (`python3 -m papaya_agent_runtime.version --next`) rather than reimplementing
  the sort in shell — `git tag` sorts lexically, which makes `v0.1.9` look newer
  than `v0.1.10`, and a release process that gets that wrong goes backwards.

Two constraints bound everything here, both inherited from `capabilities.py`, which
is read at connect time by a client that gives the answer ten seconds:

**It cannot fail.** No call in this module raises. Every failure — no git binary, a
directory that is not a checkout, a repository with no commits, a git that hangs —
becomes a version string. "I could not tell you" is a worse answer than a version
that is visibly a fallback.

**It cannot be slow.** `git describe --dirty` walks the working tree, so its timeout
is the generous one; when it is exceeded the answer still names the commit, via a
`git rev-parse` that touches nothing but `HEAD`. A flat `0.0.0` is reserved for the
cases where there is genuinely nothing to name: no git, not a checkout, or both
commands failing.

It is also importable by a bare, possibly old, system interpreter — `bin/ppy`
answers `capabilities` that way on a checkout nobody has synced yet — so nothing
here imports the client, the state database, or anything outside the standard
library.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

#: What a version is when nothing at all can be read: no git, not a checkout, or
#: both git invocations failing. Deliberately a valid PEP 440 version, so every
#: consumer can parse what it gets without a special case for "unknown".
FALLBACK_VERSION = "0.0.0"

#: Release tags carry a `v`. Nothing else in the repository's tag namespace does,
#: which is what lets `desktop-v1` and friends be ignored without a deny-list.
TAG_PREFIX = "v"

#: The glob handed to git, and the strict form applied to what comes back. The
#: glob is the looser of the two on purpose — git's `--match` is fnmatch, so it
#: cannot express "exactly three numbers" — and the regex is what actually decides.
TAG_GLOB = "v[0-9]*.[0-9]*.[0-9]*"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

#: The first release, for a repository that has never been tagged. Not `0.0.1`:
#: `0.0.x` reads as "no version yet", which is the thing this whole module exists
#: to stop the desktop card from saying.
FIRST_RELEASE = (0, 1, 0)

#: `--dirty` walks the working tree, so a large or cold checkout can take a while;
#: `rev-parse` reads one ref and cannot. Neither is a latency budget — the answer
#: is milliseconds in practice — they are the point at which a hung git stops
#: being worth waiting for, well under the client's ten-second connect probe.
DESCRIBE_TIMEOUT_SECONDS = 2.0
REV_PARSE_TIMEOUT_SECONDS = 1.0

#: Reading tags happens in CI, where latency does not matter and a wrong answer
#: would tag the wrong release, so it waits far longer before giving up.
TAG_TIMEOUT_SECONDS = 30.0

#: `--abbrev=7` rather than git's default: core.abbrev scales with how many objects
#: a repository has, so an unpinned length would silently grow and the version
#: string for one commit would differ between two clones of it.
DESCRIBE_ARGS: tuple[str, ...] = (
    "describe",
    "--tags",
    "--match",
    TAG_GLOB,
    "--long",
    "--dirty",
    "--always",
    "--abbrev=7",
)

#: `v0.1.3-2-gabc1234[-dirty]` — the long form, which `--long` guarantees even when
#: HEAD *is* the tag (distance 0).
_DESCRIBED = re.compile(
    r"^v(?P<release>\d+\.\d+\.\d+)-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+)(?P<dirty>-dirty)?$"
)
#: `abc1234[-dirty]` — what `--always` falls back to when no tag matches.
_BARE = re.compile(r"^(?P<sha>[0-9a-f]{4,40})(?P<dirty>-dirty)?$")

#: Variables that would send git somewhere other than the directory it was asked
#: about. They are set inside a git hook, and a runtime that answered with the
#: version of whatever repository invoked the hook would be quietly wrong.
_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")

#: The checkout this code is running from: `<root>/src/papaya_agent_runtime/version.py`.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── talking to git ──────────────────────────────────────────────────────────


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """The only place this module starts a process, and so the only test seam.

    Every way git can disappoint — absent, hung, pointed elsewhere by a hook's
    environment — is reachable by replacing this one function.
    """
    env = {key: value for key, value in os.environ.items() if key not in _REDIRECTS}
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _git(root: str, args: tuple[str, ...], timeout: float) -> str | None:
    """Git's trimmed stdout, or ``None`` for any way of not getting one.

    A failure is not distinguished from a refusal on purpose: "there is no git
    here", "this is not a checkout" and "git took too long" all mean the same
    thing to every caller, which is that the answer has to come from somewhere
    else.
    """
    try:
        proc = _run(["git", "-C", root, *args], timeout)
    except (OSError, subprocess.SubprocessError):
        # OSError covers no git binary; SubprocessError covers the timeout.
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


# ── the version of this checkout ────────────────────────────────────────────


def _pep440(release: str | None, distance: int, sha: str, dirty: bool) -> str:
    """Assemble the version, with the local segment carrying everything inexact.

    PEP 440 has no room in the public part of a version for "and some edits":
    `0.1.3+dirty` would claim to *be* release 0.1.3, which is the one thing a
    modified checkout is not. So the commit and the dirt both live in the local
    segment, and a dirty tagged head is `0.1.3+g<sha7>.dirty` — a version that
    sorts as 0.1.3 and says, to anyone reading it, that it is not quite 0.1.3.

    Only the exact tag, on a clean tree, gets to be a bare `X.Y.Z`.

    "Dirty" is git's own meaning: a *tracked* file differs from HEAD. Untracked
    files do not count, which is what anybody would want — a worker's scratch
    directory beside the source does not change which build is running.
    """
    local: list[str] = []
    if release is None:
        public = FALLBACK_VERSION
        local.append(f"g{sha}")
    elif distance:
        public = f"{release}.post{distance}"
        local.append(f"g{sha}")
    else:
        public = release
        if dirty:
            local.append(f"g{sha}")
    if dirty:
        local.append("dirty")
    return f"{public}+{'.'.join(local)}" if local else public


def _from_describe(described: str) -> str | None:
    """A version from `git describe` output, or ``None`` if it is a shape we do not model."""
    match = _DESCRIBED.match(described)
    if match:
        return _pep440(match["release"], int(match["distance"]), match["sha"], bool(match["dirty"]))
    match = _BARE.match(described)
    if match:
        return _pep440(None, 0, match["sha"], bool(match["dirty"]))
    return None


def derive(root: str | None = None) -> str:
    """The PEP 440 version of the checkout at ``root``. Never raises.

    Two invocations, in order of how much they know and how much they cost:

    1. ``git describe --tags --match <glob> --long --dirty --always --abbrev=7``
       answers everything at once — nearest release tag, distance from it, the
       abbreviated commit, and whether the tree is dirty.
    2. ``git rev-parse --short=7 HEAD`` is asked only when the first did not
       answer usably: it hung, it failed, or it named a tag the glob allows but
       the strict form does not (`v0.1.3-rc1`). Naming the commit is still far
       better than naming nothing, so the answer degrades to `0.0.0+g<sha7>`
       rather than to a flat `0.0.0`.

    A flat ``0.0.0`` therefore means what it says: there is no git, this is not a
    checkout, or the repository has no commit to point at.
    """
    root = ROOT if root is None else root
    described = _git(root, DESCRIBE_ARGS, DESCRIBE_TIMEOUT_SECONDS)
    if described is not None:
        version_string = _from_describe(described)
        if version_string is not None:
            return version_string
    sha = _git(root, ("rev-parse", "--short=7", "HEAD"), REV_PARSE_TIMEOUT_SECONDS)
    if sha is None or not _BARE.match(sha):
        return FALLBACK_VERSION
    # Dirt is only known if describe got far enough to say so; a `rev-parse`
    # reached because describe timed out has no opinion, and inventing one
    # either way would be worse than leaving it off.
    return _pep440(None, 0, sha, dirty=described is not None and described.endswith("-dirty"))


_CACHE: dict[str, str] = {}


def version(root: str | None = None) -> str:
    """:func:`derive`, computed once per checkout per process.

    `__version__` is read on every `ppy` invocation and on every supervised
    `hello`, and the answer cannot change inside a process in any way worth a
    second subprocess — except one. A fallback is *not* cached: it can mean "git
    was busy for two seconds", and a runtime that answered `0.0.0` for the rest
    of its life because of one loaded moment would be exactly the bug this
    module was written to remove.
    """
    key = os.path.abspath(ROOT if root is None else root)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    computed = derive(key)
    if computed != FALLBACK_VERSION:
        _CACHE[key] = computed
    return computed


def cache_clear() -> None:
    """Forget every remembered version. For tests that move a repository's tags."""
    _CACHE.clear()


# ── the version of the next release ─────────────────────────────────────────


class GitUnreadable(RuntimeError):
    """Git could not be read where reading it is the whole point (the release path)."""


def parse_tag(name: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` for a release tag, or ``None`` for anything else.

    Strict on purpose. `desktop-v1`, `v0.2`, `v1.0.0-rc1` and `nightly` are all
    somebody else's tags, and a release process that bumped from one of them
    would name a release nobody asked for.
    """
    match = TAG_RE.match(name.strip())
    if match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def releases(names: list[str] | tuple[str, ...]) -> list[tuple[int, int, int]]:
    """Every release tag among ``names``, sorted oldest first — numerically.

    The sort is the reason this is Python and not a shell pipeline: `git tag`
    orders lexically, so it puts `v0.1.10` *before* `v0.1.9` and the eleventh
    patch of a line would be released as the tenth, forever.
    """
    found = [parsed for parsed in (parse_tag(name) for name in names) if parsed is not None]
    return sorted(set(found))


def newest_release(names: list[str] | tuple[str, ...]) -> tuple[int, int, int] | None:
    """The highest release among ``names``, or ``None`` if there is not one."""
    found = releases(names)
    return found[-1] if found else None


def next_release(names: list[str] | tuple[str, ...]) -> tuple[int, int, int]:
    """The release that follows the newest of ``names``: one patch on, or the first."""
    newest = newest_release(names)
    if newest is None:
        return FIRST_RELEASE
    major, minor, patch = newest
    return (major, minor, patch + 1)


def format_release(release: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in release)


def next_version(names: list[str] | tuple[str, ...]) -> str:
    """The next release as a version string: `0.1.5`."""
    return format_release(next_release(names))


def next_tag(names: list[str] | tuple[str, ...]) -> str:
    """The next release as a tag name: `v0.1.5`."""
    return f"{TAG_PREFIX}{next_version(names)}"


def all_tags(root: str | None = None) -> list[str] | None:
    """Every tag in the repository that could be a release, or ``None`` if git failed.

    ``None`` matters here in a way it does not for :func:`derive`. An empty list
    means "this repository has never been released", and the caller answers
    `v0.1.0` to it; if a git that could not be read produced the same empty list,
    a shallow clone would re-release `v0.1.0` over an existing history.
    """
    root = ROOT if root is None else root
    listed = _git(root, ("tag", "--list", TAG_GLOB), TAG_TIMEOUT_SECONDS)
    if listed is None:
        return None
    return listed.split()


def tags_at_head(root: str | None = None) -> list[str] | None:
    """The release tags on HEAD, or ``None`` if git failed.

    This is the idempotence check. A commit that already carries a release tag
    has already been released — by an earlier run of the workflow, by a re-run of
    this one, or by a person cutting a minor by hand — and tagging it again would
    either fail or invent a second name for one build.
    """
    root = ROOT if root is None else root
    listed = _git(root, ("tag", "--points-at", "HEAD"), TAG_TIMEOUT_SECONDS)
    if listed is None:
        return None
    return [name for name in listed.split() if parse_tag(name) is not None]


def release_tag_to_create(root: str | None = None) -> str | None:
    """The tag the next release should carry, or ``None`` when there is nothing to do.

    ``None`` is the "already tagged" answer, and it is the whole of the workflow's
    idempotence: the same push, re-run, sees the tag its first run created and
    stops. Raises :class:`GitUnreadable` rather than guessing, because in CI a
    guess would be a wrong release.
    """
    head = tags_at_head(root)
    if head is None:
        raise GitUnreadable("could not read the tags on HEAD")
    if head:
        return None
    names = all_tags(root)
    if names is None:
        raise GitUnreadable("could not list this repository's tags")
    return next_tag(names)


# ── command line ────────────────────────────────────────────────────────────


USAGE = """usage: python3 -m papaya_agent_runtime.version [--next]

  (no arguments)  print the PEP 440 version of this checkout
  --next          print the tag the next release should carry, or nothing at all
                  when HEAD already carries a release tag
"""


def main(argv: list[str] | None = None) -> int:
    """Print a version. Also `.github/workflows/release.yml`'s only computation.

    `--next` prints one line and exits 0 whether or not there is a tag to create,
    because "already released" is a success: the workflow reads the line, and an
    empty one means stop. Exit 2 is reserved for a git it could not read at all,
    which in CI is a broken checkout rather than a release that should proceed.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE.rstrip())
        return 0
    if args == ["--next"]:
        try:
            tag = release_tag_to_create()
        except GitUnreadable as exc:
            print(f"papaya_agent_runtime.version: {exc}", file=sys.stderr)
            return 2
        print(tag or "")
        return 0
    if args:
        print(USAGE.rstrip(), file=sys.stderr)
        return 2
    print(version())
    return 0


__all__ = [
    "DESCRIBE_ARGS",
    "FALLBACK_VERSION",
    "FIRST_RELEASE",
    "TAG_GLOB",
    "TAG_PREFIX",
    "TAG_RE",
    "GitUnreadable",
    "all_tags",
    "cache_clear",
    "derive",
    "main",
    "newest_release",
    "next_release",
    "next_tag",
    "next_version",
    "parse_tag",
    "release_tag_to_create",
    "releases",
    "tags_at_head",
    "version",
]


if __name__ == "__main__":
    raise SystemExit(main())
