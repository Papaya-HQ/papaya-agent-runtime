"""Finding repositories to work on, and learning one properly once it is registered.

A runtime that waits to be told which repositories exist is a runtime that sits
idle. Two halves here, and they are deliberately different in character:

**Discovery is an offer, never a scan.** Candidates come from the forge — the
organizations the person actually belongs to, through `gh` — not from walking the
filesystem. A repository on disk that nobody mentioned is not a signal; a repository
their team pushed to yesterday is. Discovery only ever *proposes*: the registration
itself stays an explicit act, because registering a repository is what makes it
something workers may change.

**Onboarding is the expensive part, done once.** A first dispatch into a repository
nobody has read is a worker guessing at the test command, the lint gate and the
branch conventions — and guessing in a worktree, slowly, on the clock. So the moment
a repository is registered, read it: how it builds, how it tests, what CI will
actually run, which contracts it carries for agents, whether it has a design system
worth matching. That goes into the repository's durable notes, where the next brief
and the next worker both read it, instead of being rediscovered every time.

Everything is derived from files in the registered base clone, so onboarding is
offline, fast, and truthful about what it could not find.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: The forge CLI discovery reads from. Already required for delivery.
GH = "gh"
#: A forge listing should never hang a session.
LIST_TIMEOUT = 45
#: How many repositories to consider per owner before ranking.
DEFAULT_LIMIT = 50
#: How many workflow commands to keep per repository — enough to see the gate,
#: not so many that the notes become a copy of the YAML.
MAX_CI_COMMANDS = 12


class SolicitError(RuntimeError):
    """Discovery could not run — the forge CLI is missing or not signed in."""


# ── Discovery ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """A repository the runtime could take on, described the way a person reads it."""

    name: str
    owner: str
    url: str
    description: str = ""
    language: str = ""
    pushed_at: str = ""
    is_fork: bool = False

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    def sentence(self) -> str:
        """One line the runtime can read out when offering this repository."""
        bits = [self.slug]
        if self.language:
            bits.append(self.language)
        if self.pushed_at:
            bits.append(f"last pushed {self.pushed_at[:10]}")
        head = " — ".join([bits[0], ", ".join(bits[1:])]) if len(bits) > 1 else bits[0]
        return f"{head}: {self.description}" if self.description else head


def _gh() -> str:
    path = shutil.which(GH)
    if path is None:
        raise SolicitError(
            "the GitHub CLI is not installed, so there is nothing to discover from. "
            "It is needed for delivery too, so install it either way."
        )
    return path


def _gh_json(args: list[str]) -> list | dict:
    argv = [_gh(), *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=LIST_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SolicitError(f"`gh {' '.join(args)}` did not answer: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        raise SolicitError(f"`gh {' '.join(args)}` failed: {tail}")
    if not proc.stdout.strip():
        return []
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise SolicitError(f"`gh {' '.join(args)}` returned something that was not JSON") from exc


def owners() -> list[str]:
    """The signed-in account plus every organization it belongs to.

    These are the places whose repositories it is reasonable to offer. Anything
    outside them the person can still name directly.
    """
    found: list[str] = []
    viewer = _gh_json(["api", "user", "--jq", ".login"])
    if isinstance(viewer, str) and viewer.strip():
        found.append(viewer.strip())
    else:
        raw = _gh_json(["api", "user"])
        if isinstance(raw, dict) and raw.get("login"):
            found.append(str(raw["login"]))
    orgs = _gh_json(["api", "user/orgs"])
    if isinstance(orgs, list):
        found.extend(str(o["login"]) for o in orgs if isinstance(o, dict) and o.get("login"))
    seen: list[str] = []
    for owner in found:
        if owner and owner not in seen:
            seen.append(owner)
    return seen


def candidates(
    *,
    owner: str | None = None,
    limit: int = DEFAULT_LIMIT,
    include_forks: bool = False,
) -> list[Candidate]:
    """Repositories worth offering, newest activity first, already-registered removed.

    Archived repositories never appear: they cannot take a pull request, so offering
    one is an offer to waste a dispatch. Forks are out by default for the same
    reason — work on a fork usually belongs upstream.
    """
    targets = [owner] if owner else owners()
    fields = "name,owner,url,description,pushedAt,primaryLanguage,isArchived,isFork"
    found: list[Candidate] = []
    for target in targets:
        rows = _gh_json(["repo", "list", target, "--limit", str(limit), "--json", fields])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("isArchived"):
                continue
            if row.get("isFork") and not include_forks:
                continue
            owner_block = row.get("owner")
            login = ""
            if isinstance(owner_block, dict):
                login = str(owner_block.get("login") or "")
            language_block = row.get("primaryLanguage")
            language = ""
            if isinstance(language_block, dict):
                language = str(language_block.get("name") or "")
            found.append(
                Candidate(
                    name=str(row.get("name") or ""),
                    owner=login or str(target),
                    url=str(row.get("url") or ""),
                    description=str(row.get("description") or "").strip(),
                    language=language,
                    pushed_at=str(row.get("pushedAt") or ""),
                    is_fork=bool(row.get("isFork")),
                )
            )
    return _rank(_without_registered(found))


def _without_registered(found: list[Candidate]) -> list[Candidate]:
    from papaya_agent_runtime import repos

    try:
        registered = repos.list_repos()
    except Exception:  # noqa: BLE001 - discovery must work before any repo exists
        return found
    taken = set()
    for row in registered:
        taken.add(str(row.get("name") or "").lower())
        slug = repos.forge_slug(row.get("forge_url") or row.get("origin"))
        if slug:
            taken.add(slug.lower())
    return [c for c in found if c.name.lower() not in taken and c.slug.lower() not in taken]


def _rank(found: list[Candidate]) -> list[Candidate]:
    """Most recently pushed first — the best available proxy for "live"."""
    return sorted(found, key=lambda c: (c.pushed_at, c.slug), reverse=True)


# ── Onboarding ──────────────────────────────────────────────────────────────


@dataclass
class Onboarding:
    """What reading a registered repository turned up."""

    name: str
    local_path: str
    origin: str = ""
    default_branch: str = ""
    stacks: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    ci_commands: list[str] = field(default_factory=list)
    ci_workflows: list[str] = field(default_factory=list)
    contracts: list[str] = field(default_factory=list)
    design: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)


#: Marker file → the stack it proves, and where its task commands live.
_STACK_MARKERS = {
    "package.json": "Node",
    "pyproject.toml": "Python",
    "uv.lock": "Python (uv)",
    "poetry.lock": "Python (poetry)",
    "requirements.txt": "Python (pip)",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "Gemfile": "Ruby",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java (Gradle)",
    "Package.swift": "Swift",
    "pubspec.yaml": "Dart/Flutter",
}

#: Files that tell a worker how this repository expects to be worked on.
_CONTRACT_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CONTRIBUTING.md",
)

#: Evidence that UI work here has a reference to match rather than invent.
_DESIGN_MARKERS = (
    "design",
    "tailwind.config.js",
    "tailwind.config.ts",
    ".storybook",
    "design-system",
    "packages/ui",
    "ui-kit",
)

#: Script names worth surfacing, in the order a brief would want them.
_SCRIPT_KEYS = ("test", "lint", "typecheck", "build", "check", "format", "dev", "start")

_RUN_LINE = re.compile(r"^\s*(?:-\s*)?run:\s*(?:\|[-+]?\s*)?(.*)$")


def inspect(name: str) -> Onboarding:
    """Read a registered repository's base clone and report how it works."""
    from papaya_agent_runtime import repos

    rows = {r["name"]: r for r in repos.list_repos()}
    row = rows.get(name)
    if row is None:
        known = ", ".join(sorted(rows)) or "none yet"
        raise SolicitError(f"repo {name!r} is not registered (registered: {known})")
    root = Path(row["local_path"])
    report = Onboarding(
        name=name,
        local_path=str(root),
        origin=str(row.get("origin") or ""),
        default_branch=str(row.get("default_branch") or ""),
    )
    if not root.is_dir():
        report.unknowns.append(f"the base clone is missing at {root}; `ppy repo sync {name}` first")
        return report

    for marker, stack in _STACK_MARKERS.items():
        if (root / marker).exists():
            report.stacks.append(stack)

    report.commands.update(_node_scripts(root))
    report.commands.update(_make_targets(root))
    report.commands.update(_python_commands(root))

    report.ci_workflows, report.ci_commands = _ci(root)

    report.contracts = [f for f in _CONTRACT_FILES if (root / f).exists()]
    report.design = [m for m in _DESIGN_MARKERS if (root / m).exists()]

    if not report.stacks:
        report.unknowns.append("no recognised build manifest, so the stack is a guess")
    if not report.commands:
        report.unknowns.append("no test or build command found; ask before briefing a gate")
    if not report.ci_commands:
        report.unknowns.append("no CI workflow commands found; a green local run may not be a gate")
    if not report.contracts:
        report.unknowns.append(
            "no AGENTS.md/CLAUDE.md/CONTRIBUTING.md, so conventions are unstated"
        )
    return report


def _node_scripts(root: Path) -> dict[str, str]:
    manifest = root / "package.json"
    if not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    runner = _node_runner(root)
    found = {}
    for key in _SCRIPT_KEYS:
        if isinstance(scripts.get(key), str):
            found[key] = f"{runner} {key}"
    return found


def _node_runner(root: Path) -> str:
    for lockfile, runner in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun run"),
    ):
        if (root / lockfile).exists():
            return runner
    return "npm run"


def _make_targets(root: Path) -> dict[str, str]:
    makefile = root / "Makefile"
    if not makefile.is_file():
        return {}
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    found = {}
    for key in _SCRIPT_KEYS:
        if re.search(rf"^{re.escape(key)}:", text, re.MULTILINE):
            found.setdefault(key, f"make {key}")
    return found


def _python_commands(root: Path) -> dict[str, str]:
    manifest = root / "pyproject.toml"
    if not manifest.is_file():
        return {}
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    prefix = "uv run " if (root / "uv.lock").exists() else ""
    found = {}
    if "[tool.pytest" in text or (root / "tests").is_dir():
        found.setdefault("test", f"{prefix}pytest")
    if "ruff" in text:
        found.setdefault("lint", f"{prefix}ruff check .")
    if "mypy" in text:
        found.setdefault("typecheck", f"{prefix}mypy .")
    return found


def _ci(root: Path) -> tuple[list[str], list[str]]:
    """Workflow names and the shell commands they run.

    Read with a line scan rather than a YAML parser: the control plane carries no
    third-party dependencies, and what a brief needs is the literal commands the
    gate runs, which survive that scan intact.
    """
    workflows_dir = root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return [], []
    names: list[str] = []
    commands: list[str] = []
    for path in sorted(workflows_dir.glob("*.y*ml")):
        names.append(path.name)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _RUN_LINE.match(line)
            if match is None:
                continue
            command = match.group(1).strip()
            if command and command not in commands:
                commands.append(command)
    return names, commands[:MAX_CI_COMMANDS]


def render_notes(report: Onboarding) -> str:
    """The durable repository notes an onboarding produces.

    Written for the next brief and the next worker, so it leads with the commands
    they will need and states plainly what could not be determined — an unknown that
    is named gets asked about, an unknown that is silently omitted gets guessed at.
    """
    lines = [f"# {report.name}", ""]
    if report.origin:
        lines.append(f"- Origin: {report.origin}")
    if report.default_branch:
        lines.append(f"- Default branch: `{report.default_branch}`")
    if report.stacks:
        lines.append(f"- Stack: {', '.join(report.stacks)}")
    lines.append("")

    lines.append("## How it builds and verifies")
    lines.append("")
    if report.commands:
        for key in _SCRIPT_KEYS:
            if key in report.commands:
                lines.append(f"- {key}: `{report.commands[key]}`")
        for key, value in sorted(report.commands.items()):
            if key not in _SCRIPT_KEYS:
                lines.append(f"- {key}: `{value}`")
    else:
        lines.append("- Not determined from the repository; ask before briefing a gate.")
    lines.append("")

    lines.append("## What CI actually runs")
    lines.append("")
    if report.ci_commands:
        lines.append(f"Workflows: {', '.join(report.ci_workflows)}")
        lines.append("")
        for command in report.ci_commands:
            lines.append(f"- `{command}`")
        lines.append("")
        lines.append(
            "A brief's verification suite should be one of these, not a local approximation of it."
        )
    else:
        lines.append("- No workflow commands found. A green local run is not proof of a gate.")
    lines.append("")

    lines.append("## Conventions and contracts")
    lines.append("")
    if report.contracts:
        for path in report.contracts:
            lines.append(f"- `{path}` — read before briefing work here.")
    else:
        lines.append("- None stated in the repository.")
    lines.append("")

    lines.append("## Design reference")
    lines.append("")
    if report.design:
        for path in report.design:
            lines.append(f"- `{path}`")
        lines.append("")
        lines.append("UI work here has a reference to match; name it in the brief.")
    else:
        lines.append("- None found. UI work needs a reference supplied in the brief.")
    lines.append("")

    if report.unknowns:
        lines.append("## Still unknown")
        lines.append("")
        for unknown in report.unknowns:
            lines.append(f"- {unknown}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


#: Where onboarding's findings end, and where the hand-written notes resume.
NOTES_MARKER = "<!-- ppy:onboarding -->"
NOTES_END = "<!-- /ppy:onboarding -->"


def write_notes(report: Onboarding) -> Path:
    """Put an onboarding into the repository's durable notes, keeping what people wrote.

    The findings live between two markers. Everything a person or a worker added
    outside them survives a re-onboarding, so running this again after a repository
    changes its build is safe rather than destructive.
    """
    from papaya_agent_runtime import memory

    memory.ensure_memory_layout()
    path = memory.repo_notes_path(report.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    block = f"{NOTES_MARKER}\n{render_notes(report)}{NOTES_END}\n"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if NOTES_MARKER in existing and NOTES_END in existing:
        head, _, rest = existing.partition(NOTES_MARKER)
        _, _, tail = rest.partition(NOTES_END)
        updated = f"{head}{block}{tail.lstrip(os.linesep)}"
    else:
        updated = f"{block}\n{existing}" if existing.strip() else block
    path.write_text(updated, encoding="utf-8")
    return path


def onboard(name: str) -> tuple[Onboarding, Path]:
    """Read a registered repository and record what was learned. The whole step."""
    report = inspect(name)
    return report, write_notes(report)


__all__ = [
    "DEFAULT_LIMIT",
    "NOTES_END",
    "NOTES_MARKER",
    "Candidate",
    "Onboarding",
    "SolicitError",
    "candidates",
    "inspect",
    "onboard",
    "owners",
    "render_notes",
    "write_notes",
]
