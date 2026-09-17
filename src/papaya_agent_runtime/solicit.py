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
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

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
    # Deliberately NOT `--jq .login`: that prints a bare unquoted string, which is
    # not JSON, so parsing it raised and discovery could never find anyone's own
    # account. Ask for the object and read the field here.
    viewer = _gh_json(["api", "user"])
    if isinstance(viewer, dict) and viewer.get("login"):
        found.append(str(viewer["login"]))
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
    #: One paragraph of what this repository covers, read from its README. The
    #: manager's second way of placing a ticket is "the agent already knows", and
    #: this is the half of that which lives on disk rather than in Papaya.
    purpose: str = ""
    #: The top-level directories, which say what the repository is made of when
    #: the README does not say what it is for.
    layout: list[str] = field(default_factory=list)
    stacks: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    ci_commands: list[str] = field(default_factory=list)
    ci_workflows: list[str] = field(default_factory=list)
    contracts: list[str] = field(default_factory=list)
    design: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    #: The gate policy read from the repository; None when the clone is missing.
    gate: GatePolicy | None = None


#: A declaration's role: the quick gate run while working and before handing back.
SCOPED = "scoped"
#: A declaration's role: the complete run, once, before a pull request.
FULL = "full"


@dataclass(frozen=True)
class Declaration:
    """One command a repository's own instructions name, and what they say it is for."""

    role: str
    command: str
    #: ``<file>:<line>``, relative to the repository root.
    source: str
    #: The line it was read from, as written, so a brief can quote it.
    quote: str


@dataclass
class GatePolicy:
    """Who runs which gate for a repository: what the environment block tells a worker.

    Only what the repository declares (a command quoted from its own instructions,
    with the file and line) and what the runtime observes (a CI workflow that runs the
    full suite, a pre-push hook that does). Until 2026-09-17 the runtime guessed from
    target names instead, and a backend's guessed local gate was its sixteen-minute
    full suite. A repository that declares nothing has an unknown policy, and readiness
    asks its owner.
    """

    local_gate: str | None = None
    local_gate_source: str | None = None
    push_hook_runs_full_suite: bool = False
    full_suite_owner: str | None = None
    full_suite_owner_source: str | None = None
    full_suite_command: str | None = None
    full_suite_command_source: str | None = None
    #: Where each fact came from, one line each, so the notes can be checked.
    evidence: list[str] = field(default_factory=list)
    #: Every command the instructions named for a gate, in reading order.
    declarations: list[Declaration] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.local_gate and not self.push_hook_runs_full_suite

    @property
    def unknown(self) -> list[str]:
        """The gate answers nobody has given: ``scoped gate``, ``full suite``, or neither."""
        missing = []
        if not self.local_gate:
            missing.append("scoped gate")
        if not self.full_suite_command:
            missing.append("full suite")
        return missing

    def sources(self) -> dict[str, str]:
        """Each answer's source, keyed the way `environment.set_fields` takes them."""
        return {
            answer: source
            for answer in ("local_gate", "full_suite_command", "full_suite_owner")
            if (source := getattr(self, f"{answer}_source"))
        }

    def describe(self) -> list[str]:
        def said(value: str | None, source: str | None) -> str:
            if not value:
                return "unknown (the repository does not say)"
            return f"`{value}` ({source})" if source else f"`{value}`"

        owner = self.full_suite_owner or "nobody"
        if self.full_suite_owner_source:
            owner = f"{owner} ({self.full_suite_owner_source})"
        return [
            f"scoped gate: {said(self.local_gate, self.local_gate_source)}",
            f"full suite: {said(self.full_suite_command, self.full_suite_command_source)}",
            f"full suite owner: {owner}",
            "pre-push hook runs the full suite: "
            + ("yes" if self.push_hook_runs_full_suite else "no"),
        ]


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

    report.purpose = _readme_paragraph(root)
    report.layout = _top_level_layout(root)

    for marker, stack in _STACK_MARKERS.items():
        if (root / marker).exists():
            report.stacks.append(stack)

    report.commands.update(_node_scripts(root))
    report.commands.update(_make_targets(root))
    report.commands.update(_python_commands(root))

    report.ci_workflows, report.ci_commands = _ci(root)

    report.contracts = [f for f in _CONTRACT_FILES if (root / f).exists()]
    report.design = [m for m in _DESIGN_MARKERS if (root / m).exists()]
    report.gate = derive_gate_policy(root, report)

    if not report.purpose:
        report.unknowns.append(
            "no README paragraph says what this repository is for, so a ticket cannot be "
            "placed here by subject; write one into the notes by hand"
        )
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


#: Where a repository says what it is, in the order people write it.
_README_FILES = ("README.md", "README.rst", "README.txt", "README", "docs/README.md")

#: Directories that say nothing about what a repository covers.
_DULL_DIRECTORIES = {
    ".git",
    ".github",
    ".idea",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
}

#: How long the recorded purpose may run. A paragraph the manager reads to place
#: a ticket, not the README itself.
MAX_PURPOSE_CHARS = 600

#: Line openings that are README structure — headings, quotes, tables, rules,
#: comments, badges, images and lists — rather than the prose that says what it is.
_NOT_PROSE = ("#", ">", "|", "---", "===", "<!--", "[!", "![", "- ", "* ", "+ ", "<")


def _readme_paragraph(root: Path) -> str:
    """The first real paragraph of the README, as one line.

    "Real" means the first block of prose that is not the title, a badge row, a
    table of contents entry or a fenced block — the sentence a person wrote to
    answer "what is this". Everything structural is skipped rather than cleaned
    up, because a paragraph that needs cleaning up is not the one worth keeping.
    """
    for candidate in _README_FILES:
        path = root / candidate
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        paragraph: list[str] = []
        fenced = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            if not stripped:
                if paragraph:
                    break
                continue
            if stripped.startswith(_NOT_PROSE):
                if paragraph:
                    break
                continue
            paragraph.append(stripped)
        prose = " ".join(paragraph).strip()
        if prose:
            return prose[:MAX_PURPOSE_CHARS].rstrip()
    return ""


def _top_level_layout(root: Path) -> list[str]:
    """The repository's top-level directories, alphabetically."""
    try:
        entries = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [name for name in entries if name not in _DULL_DIRECTORIES and not name.startswith(".")]


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


# ── Gate policy ─────────────────────────────────────────────────────────────

#: The runtime's gate guesses before 2026-09-17, kept only so the start remedy can
#: recognise a stored value that came from them (`clear_heuristic_gate_policies`).
_LEGACY_FAST_MAKE_TARGETS = (
    "test-fast",
    "fast-test",
    "test-unit",
    "unit-test",
    "unit",
    "test-quick",
)
_LEGACY_FAST_SCRIPTS = ("test:unit", "test:fast", "test:quick")
_LEGACY_FULL_MAKE_TARGETS = ("verify", "test")
#: The owner the old derivation wrote when no CI and no hook ran a suite.
_LEGACY_SUPERVISOR_OWNER = "supervisor (`ppy gate run --full`)"

#: Where a repository tells people and agents how to test it, in reading order.
INSTRUCTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "TESTING.md",
    "docs/TESTING.md",
    "docs/testing.md",
    "docs/development.md",
)

#: The first word of a backticked span that makes it a command rather than a name.
_PROGRAMS = frozenset(
    {
        "bun",
        "bundle",
        "cargo",
        "composer",
        "dotnet",
        "go",
        "gradle",
        "hatch",
        "just",
        "make",
        "mix",
        "mvn",
        "mypy",
        "nox",
        "npm",
        "npx",
        "nx",
        "pnpm",
        "poetry",
        "pytest",
        "python",
        "python3",
        "rake",
        "ruff",
        "tox",
        "turbo",
        "uv",
        "uvx",
        "yarn",
    }
)
#: A command written without backticks: only the shapes whose extent is unambiguous.
_BARE_COMMAND = re.compile(
    r"\b(?:make|just)\s+[A-Za-z0-9][\w:.-]*|\b(?:pnpm|yarn|bun|npm)\s+(?:run\s+)?[A-Za-z][\w:.-]*"
)
_BACKTICKED = re.compile(r"`([^`\n]+)`")

#: What the instructions say a command is for, in their own words.
_FULL_CUE = re.compile(
    r"before\s+(?:opening\s+|you\s+open\s+|raising\s+|sending\s+)?(?:a|an|the|any|your)?\s*"
    r"(?:pr\b|pull\s+request|merg|push|ship|release|review)"
    r"|full\s+(?:test\s+)?(?:suite|gate|verification|check)|whole\s+suite|entire\s+suite"
    r"|complete\s+suite|pre-?pr\b|final\s+check|everything\s+ci",
    re.IGNORECASE,
)
_SCOPED_CUE = re.compile(
    r"while\s+(?:you(?:'re|\s+are)?\s+)?(?:work|develop|iterat|cod|chang)"
    r"|as\s+you\s+(?:go|work)|during\s+development|inner\s+loop|iterat"
    r"|\bquick|\bfast\b|\bscoped\b|\btargeted\b|local\s+gate"
    r"|before\s+(?:each\s+|every\s+)?commit|after\s+(?:each|every)\s+change",
    re.IGNORECASE,
)


def _role(text: str) -> str | None:
    """The role ``text`` gives a command, judged by the cue that appears first in it."""
    full, scoped = _FULL_CUE.search(text), _SCOPED_CUE.search(text)
    if full and scoped:
        return FULL if full.start() < scoped.start() else SCOPED
    return FULL if full else SCOPED if scoped else None


def _is_command(span: str) -> bool:
    words = span.strip().lstrip("$ ").split()
    if not words:
        return False
    first = words[0]
    return first in _PROGRAMS or first.startswith(("./", "bin/", "scripts/"))


def _commands_in(line: str) -> list[tuple[int, int, str]]:
    """``(start, end, command)`` for every command on a prose line, in order."""
    found: list[tuple[int, int, str]] = []
    covered: list[tuple[int, int]] = []
    for match in _BACKTICKED.finditer(line):
        covered.append(match.span())
        if _is_command(match.group(1)):
            found.append((match.start(), match.end(), match.group(1).strip().lstrip("$ ")))
    for match in _BARE_COMMAND.finditer(line):
        if any(start <= match.start() < end for start, end in covered):
            continue
        found.append((match.start(), match.end(), match.group(0).strip()))
    return sorted(found)


def _clause(text: str, *, after: bool) -> str:
    """The sentence fragment next to a command: up to a sentence end, on its side."""
    parts = re.split(r"(?<=[.!?])\s+|;\s+", text)
    return parts[0] if after else parts[-1]


def declarations(root: Path) -> list[Declaration]:
    """Every gate command a repository's instructions name, with its role and source.

    Read by line, in the order of :data:`INSTRUCTION_FILES`. A command's role comes from
    the words beside it: first the clause after it ("`make lint-check` while working"),
    then the clause before it ("before a PR, run `make verify`"), then a lead-in line
    ending in a colon, then the heading it sits under. A command no words give a role
    is not a declaration: the runtime does not guess one.
    """
    found: list[Declaration] = []
    for name in INSTRUCTION_FILES:
        path = root / name
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        heading = ""
        lead_in = ""
        fenced = False
        for number, raw in enumerate(lines, start=1):
            line = raw.strip()
            if line.startswith(("```", "~~~")):
                fenced = not fenced
                continue
            source = f"{name}:{number}"
            if fenced:
                command, _, comment = line.lstrip("$ ").partition(" #")
                if _is_command(command):
                    role = _role(comment) or _role(lead_in) or _role(heading)
                    if role:
                        found.append(Declaration(role, command.strip(), source, raw.strip()))
                continue
            if line.startswith("#"):
                heading, lead_in = line.lstrip("#").strip(), ""
                continue
            listed = bool(re.match(r"^(?:[-*+]|\d+[.)])\s", line))
            commands = _commands_in(line)
            for index, (start, end, command) in enumerate(commands):
                following = commands[index + 1][0] if index + 1 < len(commands) else len(line)
                preceding = commands[index - 1][1] if index else 0
                role = _role(_clause(line[end:following], after=True)) or _role(
                    _clause(line[preceding:start], after=False)
                )
                if role is None and listed:
                    # A bare list item takes the role of the line that introduces it.
                    role = _role(lead_in) or _role(heading)
                if role:
                    found.append(Declaration(role, command, source, line))
            if line and not listed:
                lead_in = line if line.endswith(":") else ""
    return found


def _workflow_lines(root: Path) -> list[tuple[str, int, str]]:
    """Every line of every CI workflow, as ``(path, line number, text)``."""
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    out: list[tuple[str, int, str]] = []
    for path in sorted(workflows.glob("*.y*ml")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        out.extend((relative, n, line) for n, line in enumerate(text.splitlines(), start=1))
    return out


def ci_runs(root: Path, command: str) -> str | None:
    """``<workflow>:<line>`` of the first workflow line that runs ``command``, if any."""
    wanted = " ".join(command.split())
    if not wanted:
        return None
    pattern = re.compile(rf"(?:^|[\s:|&;]){re.escape(wanted)}(?:$|[\s;&|])")
    for relative, number, line in _workflow_lines(root):
        text = " ".join(line.split())
        if text.startswith("#"):
            continue
        if pattern.search(text):
            return f"{relative}:{number}"
    return None


#: A command that runs a test suite, as a hook or a workflow spells it.
_SUITE_COMMAND = re.compile(
    r"\bpytest\b|\bmake\s+(?:test|verify)[\w-]*|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b"
    r"|\bgo\s+test\b|\bcargo\s+test\b|\bvitest\b|\bjest\b|\btox\b|\bnox\b"
)

_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.MULTILINE)


def _make_target_names(root: Path) -> set[str]:
    try:
        text = (root / "Makefile").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return set(_MAKE_TARGET.findall(text))


def _package_script_names(root: Path) -> set[str]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return {str(key) for key in scripts} if isinstance(scripts, dict) else set()


def _section(text: str, key: str) -> str:
    """The lines under a top-level YAML key, by line scan (no YAML parser here)."""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if re.match(rf"^{re.escape(key)}\s*:", line):
            inside = True
            continue
        if inside and line and not line[0].isspace() and not line.startswith("#"):
            break
        if inside:
            out.append(line)
    return "\n".join(out)


def _pre_commit_pre_push(text: str) -> str:
    """The hooks of a pre-commit config that run at pre-push."""
    if re.search(r"^default_stages:.*pre-push", text, re.MULTILINE):
        return text
    chunks = re.split(r"^\s*-\s+id:", text, flags=re.MULTILINE)
    return "\n".join(chunk for chunk in chunks[1:] if "pre-push" in chunk)


def _pre_push_hooks(root: Path) -> list[tuple[str, str]]:
    """Every pre-push hook this clone runs or ships, as (where, what it runs)."""
    found: list[tuple[str, str]] = []
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "hooks/pre-push"],
        capture_output=True,
        text=True,
        check=False,
    )
    installed = proc.stdout.strip() if proc.returncode == 0 else ""
    candidates: list[tuple[str, Path]] = []
    if installed:
        path = Path(installed)
        candidates.append((installed, path if path.is_absolute() else root / path))
    candidates.extend((name, root / name) for name in (".husky/pre-push", ".githooks/pre-push"))
    seen: set[Path] = set()
    for label, path in candidates:
        try:
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            found.append((label, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    for name in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        section = _section(text, "pre-push")
        if section.strip():
            found.append((f"{name} pre-push", section))
    try:
        text = (root / ".pre-commit-config.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    hooks = _pre_commit_pre_push(text) if text else ""
    if hooks.strip():
        found.append((".pre-commit-config.yaml pre-push stage", hooks))
    return found


def derive_gate_policy(root: Path, report: Onboarding | None = None) -> GatePolicy:
    """Read a repository's gate policy from what it declares, and nothing else.

    - **Scoped gate / full suite:** the first command the repository's own instructions
      (:data:`INSTRUCTION_FILES`) give each role, quoted with its file and line
      (:func:`declarations`). A Makefile target or package script is not a declaration
      by its name alone; it is only a command a declaration may name.
    - **Owner:** ``ci`` when a CI workflow runs the declared full suite (with the
      workflow line), else ``supervisor``: the runtime runs it once at the delivered head.
    - **Hook:** a pre-push hook (the one git runs here, honouring ``core.hooksPath``, or
      one the repository ships through husky, ``.githooks``, lefthook or pre-commit)
      whose text runs a test suite sets the hook flag.

    Whatever is not declared stays unknown (:attr:`GatePolicy.unknown`); readiness
    asks the owner rather than the runtime guessing. ``report`` is accepted for
    callers that already inspected the repository; nothing here needs it.
    """
    policy = GatePolicy()
    for where, text in _pre_push_hooks(root):
        match = _SUITE_COMMAND.search(text)
        if match:
            policy.push_hook_runs_full_suite = True
            policy.evidence.append(f"pre-push hook {where} runs `{match.group(0)}`")
            break

    policy.declarations = declarations(root)
    for found in policy.declarations:
        if found.role == SCOPED and not policy.local_gate:
            policy.local_gate, policy.local_gate_source = found.command, found.source
            policy.evidence.append(f"scoped gate from {found.source}: {found.quote}")
        elif found.role == FULL and not policy.full_suite_command:
            policy.full_suite_command = found.command
            policy.full_suite_command_source = found.source
            policy.evidence.append(f"full suite from {found.source}: {found.quote}")

    if policy.full_suite_command:
        workflow = ci_runs(root, policy.full_suite_command)
        if workflow:
            policy.full_suite_owner, policy.full_suite_owner_source = "ci", workflow
            policy.evidence.append(f"CI runs the full suite: {workflow}")
        else:
            policy.full_suite_owner = "supervisor"
            policy.full_suite_owner_source = "no CI workflow runs it"
    return policy


def legacy_gate_policy(root: Path, report: Onboarding) -> GatePolicy:
    """What the runtime's gate heuristics (before 2026-09-17) would have stored.

    Never used to decide a gate. The start remedy compares a stored answer with no
    source against it: a match is a guess, and is cleared.
    """
    policy = GatePolicy()
    targets = _make_target_names(root) if (root / "Makefile").is_file() else set()
    scripts = _package_script_names(root)
    fast = next((t for t in _LEGACY_FAST_MAKE_TARGETS if t in targets), None)
    script = next((s for s in _LEGACY_FAST_SCRIPTS if s in scripts), None)
    if fast:
        policy.local_gate = f"make {fast}"
    elif script:
        policy.local_gate = f"{_node_runner(root)} {script}"
    elif report.commands.get("test"):
        policy.local_gate = report.commands["test"]
    full = next((t for t in _LEGACY_FULL_MAKE_TARGETS if t in targets), None)
    policy.full_suite_command = f"make {full}" if full else report.commands.get("test")
    hooked = any(_SUITE_COMMAND.search(text) for _where, text in _pre_push_hooks(root))
    if any(_SUITE_COMMAND.search(c) for c in report.ci_commands):
        policy.full_suite_owner = "ci"
    elif hooked:
        policy.full_suite_owner = "pre-push hook"
    elif policy.full_suite_command:
        policy.full_suite_owner = _LEGACY_SUPERVISOR_OWNER
    return policy


def has_gate_policy(row) -> bool:
    """Does a registered repository say both its scoped gate and its full suite?"""
    return not gate_unknowns(row)


def gate_unknowns(row) -> list[str]:
    """Which of ``scoped gate`` and ``full suite`` a registered repository leaves unsaid."""
    missing = []
    if not str(row.get("local_gate") or "").strip():
        missing.append("scoped gate")
    if not str(row.get("full_suite_command") or "").strip():
        missing.append("full suite")
    return missing


def apply_gate_policy(
    name: str, policy: GatePolicy, *, local_gate: str | None = None
) -> GatePolicy:
    """Store ``policy`` for ``name`` where nothing is set yet, and return what is stored.

    A value somebody set with ``ppy repo set`` is theirs and is never replaced by what
    the repository declares; an explicit ``local_gate`` (``ppy repo onboard
    --local-gate``) always is, as the person's.
    """
    from papaya_agent_runtime import environment
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        row = store.get_repo(conn, name)
        if row is None:
            raise SolicitError(f"repo {name!r} is not registered")
        fields: dict[str, object] = {}
        sources = policy.sources()
        if local_gate and local_gate.strip():
            fields["local_gate"] = local_gate.strip()
            sources.pop("local_gate", None)
        elif not row["local_gate"] and policy.local_gate:
            fields["local_gate"] = policy.local_gate
        if not row["push_hook_runs_full_suite"] and policy.push_hook_runs_full_suite:
            fields["push_hook_runs_full_suite"] = "yes"
        if not row["full_suite_owner"] and policy.full_suite_owner:
            fields["full_suite_owner"] = policy.full_suite_owner
        if not row["full_suite_command"] and policy.full_suite_command:
            fields["full_suite_command"] = policy.full_suite_command
        environment.set_fields(conn, name, sources=sources, **fields)
        stored = environment.for_repo(store.get_repo(conn, name))
    finally:
        conn.close()
    return GatePolicy(
        local_gate=stored.local_gate,
        local_gate_source=stored.local_gate_source,
        push_hook_runs_full_suite=stored.push_hook_runs_full_suite,
        full_suite_owner=stored.full_suite_owner if stored.full_suite_command else None,
        full_suite_owner_source=stored.full_suite_owner_source,
        full_suite_command=stored.full_suite_command,
        full_suite_command_source=stored.full_suite_command_source,
        evidence=list(policy.evidence),
        declarations=list(policy.declarations),
    )


def clear_heuristic_gate_policies() -> list[str]:
    """The start remedy: drop every stored gate answer the old heuristics guessed.

    An answer marked ``heuristic`` is dropped. An answer with no source at all predates
    sources: when it equals what :func:`legacy_gate_policy` gives for the base clone it
    was guessed, so it is marked ``heuristic`` and dropped; otherwise a person set it,
    and it is marked ``person`` and kept. A repository that lost an answer is read again
    for what it declares. Returns one line per repository changed; never raises. Each
    change is also a `config_change` event.
    """
    from papaya_agent_runtime import config_changes, environment, repos
    from papaya_agent_runtime.state import init_db, store

    lines: list[str] = []
    try:
        rows = repos.list_repos()
    except Exception as exc:  # noqa: BLE001 - a remedy never stops the runtime
        return [f"could not read registered repositories for their gate policies: {exc}"]
    for row in rows:
        name = str(row.get("name") or "")
        try:
            line = _clear_heuristic_policy(row, config_changes, environment, init_db, store)
        except Exception as exc:  # noqa: BLE001 - one repository's remedy never stops the rest
            line = f"{name}: could not check its gate policy for guessed answers: {exc}"
        if line:
            lines.append(line)
    return lines


def _clear_heuristic_policy(row, config_changes, environment, init_db, store) -> str | None:
    name = str(row["name"])
    root = Path(str(row.get("local_path") or ""))
    legacy: GatePolicy | None = None
    dropped: list[tuple[str, str]] = []
    kept: dict[str, str] = {}
    for answer in environment.GATE_ANSWERS:
        value = str(row.get(answer) or "").strip()
        if not value:
            continue
        source = str(row.get(environment.source_column(answer)) or "").strip()
        if source == environment.SOURCE_HEURISTIC:
            dropped.append((answer, value))
            continue
        if source:
            continue
        if legacy is None and root.is_dir():
            legacy = legacy_gate_policy(root, inspect(name))
        guessed = (legacy is not None and getattr(legacy, answer) == value) or (
            answer == "full_suite_owner" and value == _LEGACY_SUPERVISOR_OWNER
        )
        if guessed:
            dropped.append((answer, value))
        elif legacy is not None:
            kept[environment.source_column(answer)] = environment.SOURCE_PERSON
    conn = init_db()
    try:
        cleared = {answer: None for answer, _ in dropped}
        cleared.update({environment.source_column(answer): None for answer, _ in dropped})
        store.update_repo_fields(conn, name, **cleared, **kept)
    finally:
        conn.close()
    if not dropped:
        return None
    for answer, value in dropped:
        config_changes.record(
            key=f"repos.{name}.{answer}",
            before=value,
            after=None,
            why=(
                f"{answer} `{value}` was guessed by the old gate heuristics, not declared by {name}"
            ),
            evidence={"repository": name, "source": environment.SOURCE_HEURISTIC},
        )
    what = ", ".join(f"{answer} `{value}`" for answer, value in dropped)
    line = f"{name}: cleared guessed gate answers ({what}); falling back to what {name} declares"
    if root.is_dir():
        stored = apply_gate_policy(name, derive_gate_policy(root))
        missing = stored.unknown
        line += (
            f": {'; '.join(stored.describe()[:3])}"
            if not missing
            else f"; still unknown: {', '.join(missing)} (readiness asks the owner)"
        )
    log.info("[solicit] %s", line)
    return line


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

    # First, because it is what a manager placing a ticket reads: which of the
    # registered repositories does this work item's subject belong to?
    lines.append("## What it is")
    lines.append("")
    if report.purpose:
        lines.append(report.purpose)
    else:
        lines.append("Not stated in a README; say what this repository covers here by hand.")
    if report.layout:
        lines.append("")
        lines.append(f"Top level: {', '.join(f'`{name}/`' for name in report.layout)}")
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

    if report.gate is not None:
        lines.append("## Gate policy")
        lines.append("")
        lines.extend(f"- {line}" for line in report.gate.describe())
        lines.extend(f"- {line}" for line in report.gate.evidence)
        if report.gate.declarations:
            lines.append("")
            lines.append("What the repository says, in its own words:")
            lines.append("")
            lines.extend(
                f"- {found.role}: `{found.command}` — {found.source}: “{found.quote}”"
                for found in report.gate.declarations
            )
            lines.append("")
        if report.gate.unknown:
            lines.append(
                f"- Unknown: {', '.join(report.gate.unknown)}. The repository's instructions do "
                "not say, so nothing is guessed: ask the owner, then record the answer in the "
                'repository\'s AGENTS.md or with `ppy repo set <name> --local-gate "..." '
                '--full-suite-command "..."`.'
            )
        lines.append(
            "- A gate that may run longer than ten minutes runs through `ppy gate run`, "
            "never as a tool call."
        )
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

#: The line `memory.seed_repo_memory` puts under "What it is" before anyone knows.
PURPOSE_PLACEHOLDER = "- One-paragraph purpose and high-level architecture."


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
    if report.purpose:
        # The seeded template's "What it is" is a placeholder nobody wrote. Filling
        # it — and only while it is still the untouched placeholder — keeps one
        # answer in the file; a person's own paragraph there is never replaced.
        updated = updated.replace(PURPOSE_PLACEHOLDER, report.purpose, 1)
    path.write_text(updated, encoding="utf-8")
    return path


def onboard(name: str, *, local_gate: str | None = None) -> tuple[Onboarding, Path]:
    """Read a registered repository and record what was learned. The whole step.

    That includes its gate policy, stored on the repository where nothing is set yet,
    so the environment block a worker is handed says the true thing.
    """
    report = inspect(name)
    if report.gate is not None or local_gate:
        report.gate = apply_gate_policy(name, report.gate or GatePolicy(), local_gate=local_gate)
    return report, write_notes(report)


def _fill_gate_policy(row) -> None:
    """Give a registered repository with no gate policy the one its files imply."""
    if has_gate_policy(row) or not Path(str(row.get("local_path") or "")).is_dir():
        return
    report = inspect(str(row["name"]))
    if report.gate is not None:
        apply_gate_policy(report.name, report.gate)


# ── Registering on demand ───────────────────────────────────────────────────


class NotYours(SolicitError):
    """The repository is real, but outside every account this person belongs to."""


@dataclass(frozen=True)
class Ensured:
    """What `ensure` did, so a caller can say it in one sentence."""

    name: str
    slug: str
    registered: bool
    onboarded: bool
    notes_path: str = ""

    def sentence(self) -> str:
        if not self.registered and not self.onboarded:
            return f"{self.name} was already registered and onboarded."
        did = []
        if self.registered:
            did.append("registered")
        if self.onboarded:
            did.append("read how it builds and tests")
        return f"{self.name} ({self.slug}): {' and '.join(did)}."


def _already_registered(spec: str) -> dict | None:
    """The registered repo this spec names, matched by name or by forge slug."""
    from papaya_agent_runtime import repos

    wanted = spec.strip().lower()
    wanted_slug = (repos.forge_slug(spec) or wanted).lower()
    for row in repos.list_repos():
        name = str(row.get("name") or "").lower()
        slug = (repos.forge_slug(row.get("forge_url") or row.get("origin")) or "").lower()
        if wanted in {name, slug} or wanted_slug in {name, slug}:
            return row
        if slug.endswith(f"/{wanted}"):
            return row
    return None


def _is_onboarded(name: str) -> bool:
    from papaya_agent_runtime import memory

    path = memory.repo_notes_path(name)
    try:
        return path.is_file() and NOTES_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def ensure(spec: str, *, allow_outside: bool = False) -> Ensured:
    """Make a repository ready to work in, registering it if it is not yet.

    Work that names a repository should not stop because nobody has registered it
    yet — an assignment arriving while nobody is at the machine has no one to ask.
    So a repository the *work itself* names, inside an account the person belongs
    to, is registered on demand: they assigned the work, and registering is a
    read-only clone plus a row. The destructive step is pushing, and that is gated
    separately by the review gate and delivery authority.

    The boundary is enforced here rather than described in prose: the candidate has
    to come back from :func:`candidates`, which only ever reads the signed-in
    account and its organisations. Anything else raises :class:`NotYours` and is a
    question for the user. ``allow_outside`` is how an explicit human "yes, that
    one" gets past it.

    Idempotent: a repository already registered is onboarded if it never was, and
    otherwise left exactly as it is.

    Afterwards the runtime's config remedies run (`config_changes.apply`): a
    repository whose gate runs a tool the worker profile had dropped gets it back
    before its first dispatch, not after a worker is refused it.
    """
    ensured = _ensure(spec, allow_outside=allow_outside)
    from papaya_agent_runtime import config_changes

    config_changes.apply(context=f"ensured {ensured.name}")
    return ensured


def _ensure(spec: str, *, allow_outside: bool) -> Ensured:
    existing = _already_registered(spec)
    if existing is not None:
        name = str(existing["name"])
        slug = forge_slug_of(existing)
        if _is_onboarded(name):
            # Onboarded before gate policies were derived (every repository registered
            # before 2026-09-16): fill the policy in, and leave everything else alone.
            _fill_gate_policy(existing)
            return Ensured(name=name, slug=slug, registered=False, onboarded=False)
        _, path = onboard(name)
        return Ensured(name=name, slug=slug, registered=False, onboarded=True, notes_path=str(path))

    match = _match_candidate(spec) if not allow_outside else None
    if match is None and not allow_outside:
        raise NotYours(
            f"{spec!r} is not registered, and it is not in your account or any "
            "organisation you belong to — so registering it is not mine to assume. "
            "Give me its URL if you want it taken on."
        )
    url = match.url if match is not None else spec
    slug = match.slug if match is not None else (_slug_from_spec(spec) or spec)

    from papaya_agent_runtime import repos

    added = repos.add_repo(url)
    _, path = onboard(added.name)
    return Ensured(
        name=added.name, slug=slug, registered=True, onboarded=True, notes_path=str(path)
    )


def forge_slug_of(row) -> str:
    from papaya_agent_runtime import repos

    return repos.forge_slug(row.get("forge_url") or row.get("origin")) or str(row.get("name") or "")


def _match_candidate(spec: str) -> Candidate | None:
    """The discovered repository this spec names, or None when it is not theirs."""
    wanted = spec.strip().lower()
    wanted_slug = (_slug_from_spec(spec) or wanted).lower()
    for candidate in candidates(include_forks=True):
        if wanted_slug == candidate.slug.lower() or wanted == candidate.name.lower():
            return candidate
    return None


def _slug_from_spec(spec: str) -> str | None:
    from papaya_agent_runtime import repos

    slug = repos.forge_slug(spec)
    if slug:
        return slug
    cleaned = spec.strip().strip("/")
    return cleaned if cleaned.count("/") == 1 else None


__all__ = [
    "DEFAULT_LIMIT",
    "Ensured",
    "NotYours",
    "NOTES_END",
    "NOTES_MARKER",
    "Candidate",
    "Onboarding",
    "SolicitError",
    "candidates",
    "ensure",
    "inspect",
    "onboard",
    "owners",
    "render_notes",
    "write_notes",
]
