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


@dataclass
class GatePolicy:
    """Who runs which gate for a repository: what the environment block tells a worker.

    Until 2026-09-16 nothing filled these in for a repository the runtime registered
    itself, so every worker was told "local gate: not set" and ran whatever the brief
    named as a tool call — including a backend suite longer than the call (PAP-213).
    """

    local_gate: str | None = None
    push_hook_runs_full_suite: bool = False
    full_suite_owner: str | None = None
    full_suite_command: str | None = None
    #: Where each fact came from, one line each, so the notes can be checked.
    evidence: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.local_gate and not self.push_hook_runs_full_suite

    def describe(self) -> list[str]:
        local = f"`{self.local_gate}`" if self.local_gate else "none found"
        full = f"`{self.full_suite_command}`" if self.full_suite_command else "none found"
        return [
            f"local gate: {local}",
            "pre-push hook runs the full suite: "
            + ("yes" if self.push_hook_runs_full_suite else "no"),
            f"full suite: {full} (owner: {self.full_suite_owner or 'nobody'})",
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

#: Make targets and package scripts that name a quicker slice of the tests, best first.
_FAST_MAKE_TARGETS = ("test-fast", "fast-test", "test-unit", "unit-test", "unit", "test-quick")
_FAST_SCRIPTS = ("test:unit", "test:fast", "test:quick")
#: Make targets that run everything a merge would, best first.
_FULL_MAKE_TARGETS = ("verify", "test")

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


def derive_gate_policy(root: Path, report: Onboarding) -> GatePolicy:
    """Read a repository's gate policy from its own files.

    - **Hook:** a pre-push hook (the one git runs here, honouring ``core.hooksPath``, or
      one the repository ships through husky, ``.githooks``, lefthook or pre-commit)
      whose text runs a test suite sets the hook flag.
    - **Local gate:** the repository's own quicker target (``make test-fast``/
      ``test-unit``, a ``test:unit`` script) when it has one, else its test command.
    - **Full suite:** ``make verify``, else ``make test``, else the test command.
    - **Owner:** CI when a workflow runs a suite, else the hook, else the supervisor
      through ``ppy gate run --full``.
    """
    policy = GatePolicy()
    for where, text in _pre_push_hooks(root):
        match = _SUITE_COMMAND.search(text)
        if match:
            policy.push_hook_runs_full_suite = True
            policy.evidence.append(f"pre-push hook {where} runs `{match.group(0)}`")
            break

    targets = _make_target_names(root) if (root / "Makefile").is_file() else set()
    scripts = _package_script_names(root)
    fast = next((t for t in _FAST_MAKE_TARGETS if t in targets), None)
    script = next((s for s in _FAST_SCRIPTS if s in scripts), None)
    if fast:
        policy.local_gate = f"make {fast}"
        policy.evidence.append(f"local gate from the Makefile's quicker target `{fast}`")
    elif script:
        runner = _node_runner(root)
        policy.local_gate = f"{runner} {script}"
        policy.evidence.append(f"local gate from package.json's quicker script `{script}`")
    elif report.commands.get("test"):
        policy.local_gate = report.commands["test"]
        policy.evidence.append("local gate is the repository's test command; no quicker target")

    full = next((t for t in _FULL_MAKE_TARGETS if t in targets), None)
    policy.full_suite_command = f"make {full}" if full else report.commands.get("test")

    ci_suite = next((c for c in report.ci_commands if _SUITE_COMMAND.search(c)), None)
    if ci_suite:
        policy.full_suite_owner = "ci"
        policy.evidence.append(f"CI runs the suite: `{ci_suite}`")
    elif policy.push_hook_runs_full_suite:
        policy.full_suite_owner = "pre-push hook"
    elif policy.full_suite_command:
        policy.full_suite_owner = "supervisor (`ppy gate run --full`)"
    return policy


def has_gate_policy(row) -> bool:
    """Does a registered repository say how its work is gated at all?"""
    return bool(str(row.get("local_gate") or "").strip() or row.get("push_hook_runs_full_suite"))


def apply_gate_policy(
    name: str, policy: GatePolicy, *, local_gate: str | None = None
) -> GatePolicy:
    """Store ``policy`` for ``name`` where nothing is set yet, and return what is stored.

    A value somebody set with ``ppy repo set`` is theirs and is never replaced by a
    derivation; an explicit ``local_gate`` (``ppy repo onboard --local-gate``) always is.
    """
    from papaya_agent_runtime import environment
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        row = store.get_repo(conn, name)
        if row is None:
            raise SolicitError(f"repo {name!r} is not registered")
        fields: dict[str, object] = {}
        if local_gate and local_gate.strip():
            fields["local_gate"] = local_gate.strip()
        elif not row["local_gate"] and policy.local_gate:
            fields["local_gate"] = policy.local_gate
        if not row["push_hook_runs_full_suite"] and policy.push_hook_runs_full_suite:
            fields["push_hook_runs_full_suite"] = "yes"
        if not row["full_suite_owner"] and policy.full_suite_owner:
            fields["full_suite_owner"] = policy.full_suite_owner
        if not row["full_suite_command"] and policy.full_suite_command:
            fields["full_suite_command"] = policy.full_suite_command
        environment.set_fields(conn, name, **fields)
        stored = environment.for_repo(store.get_repo(conn, name))
    finally:
        conn.close()
    return GatePolicy(
        local_gate=stored.local_gate,
        push_hook_runs_full_suite=stored.push_hook_runs_full_suite,
        full_suite_owner=stored.full_suite_owner,
        full_suite_command=stored.full_suite_command,
        evidence=list(policy.evidence),
    )


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
    """
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
