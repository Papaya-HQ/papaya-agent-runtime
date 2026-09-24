"""`ppy setup`: one command from a fresh clone to a machine that takes requests.

Setting a machine up used to be a page of commands to copy: build the environment,
sign in to Claude Code and `gh`, connect to Papaya, register each repository. This
runs them in order and says nothing about what is already fine beyond one line per
step, so a re-run on a finished machine is a short list of ticks. The only things a
person answers are the workspace and agent (asked by the Papaya client, or in the
approval for a device code) and the repositories.

Every step checks first and acts only when it has to:

1. the machine: platform, git, uv and this checkout's environment (built from
   its current lockfile, with the picker's packages importing);
2. Claude Code signed in (`claude auth login` with the terminal attached if not);
3. GitHub signed in (`gh auth login`, then `gh auth setup-git`);
4. Papaya connected, on the terminal so the client asks the workspace and agent
   inside one sign-in (a device code over SSH or with no display);
5. at least one repository registered, chosen in a picker.

A step that cannot finish stops the run with one line naming the one thing to do,
so re-running picks up exactly there. Everything the flow touches outside this
process goes through three seams — :class:`Shell`, :class:`Picker` and the
``platform``/``env`` arguments — so the tests never sign anything in.

``--non-interactive`` with ``--agent``/``--workspace``/``--repo`` runs the same steps
with no prompts and never attaches a terminal. ``--non-interactive`` without them is
the profile-only path scripts have always used (``wizard.run_setup``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

#: The last line of a finished setup.
DONE = "Done. Start it with: ./bin/ppy serve"
#: How a re-run with repositories already registered describes them.
REPOS_LINE = "✓ {n} {noun} (./bin/ppy setup --repos to change)"
#: Said when the picker leaves a registered repository unticked.
KEPT_LINE = "Unticking a registered repository does not remove it; it stays registered."
#: Said when a switch ends on the agent already connected because it is the only one.
ONLY_AGENT = (
    "{agent} is the only agent you can connect in {workspace}. To use another, create it "
    "in Papaya (Agents → New agent), then run ./bin/ppy setup again."
)
#: Said when a question meets the end of stdin (no terminal, nothing piped in).
NO_TERMINAL = (
    "Setup needs a terminal to answer its questions; from a script, run "
    "./bin/ppy setup --non-interactive --repo <url>"
)
#: The owner-list entries that are not owners.
TYPE_URL = "Enter a repository URL"
FINISHED = "done"

WINDOWS_STEPS = (
    "Native Windows is not supported: the runtime locks its state with fcntl. Use WSL2:\n"
    "  1. In PowerShell as administrator: wsl --install -d Ubuntu\n"
    "  2. Restart Windows, open Ubuntu, and clone this repository under ~/ (not /mnt/c)\n"
    "  3. Run ./bin/ppy setup there"
)
UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"
CLAUDE_INSTALL = "npm install -g @anthropic-ai/claude-code"
GITHUB_HOST = "github.com"


class Stop(Exception):
    """A step that cannot finish. The message is the one line the person reads."""


@dataclass
class Options:
    """What the command line asked for."""

    interactive: bool = True
    agent: str | None = None
    workspace: str | None = None
    #: Repositories to register without the picker (URLs or ``owner/name``).
    repos: tuple[str, ...] = ()
    #: Reopen the picker even though repositories are registered.
    pick_repos: bool = False
    #: The manager-profile overrides (`--manager-provider` …), for a first config.
    profile: dict[str, Any] = field(default_factory=dict)
    skip_tools: bool = False


# ── the seams ────────────────────────────────────────────────────────────────


class Shell:
    """Every program setup runs, behind one object the tests replace."""

    def which(self, name: str) -> str | None:
        import shutil

        return shutil.which(name)

    def capture(self, argv: list[str], timeout: float = 60.0) -> tuple[int, str]:
        """Exit status and combined output; 127 when the program cannot be run."""
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def attach(self, argv: list[str]) -> int:
        """Run ``argv`` on the person's own terminal: its stdin, stdout and stderr.

        Sign-in flows (`claude auth login`, `gh auth login`) ask questions and print
        codes, so they get the terminal rather than a pipe.
        """
        try:
            return subprocess.run(argv, check=False).returncode  # noqa: S603
        except OSError:
            return 127


@dataclass(frozen=True)
class Repo:
    """One row of `gh repo list`."""

    slug: str
    description: str = ""
    updated_at: str = ""

    @property
    def title(self) -> str:
        return f"{self.slug} — {self.description}" if self.description else self.slug


class Picker:
    """The questions setup asks. :class:`QuestionaryPicker` on a terminal."""

    def yes_no(self, question: str, default: bool = False) -> bool:
        raise NotImplementedError

    def owner(self, owners: list[str], selected: int, default: str | None) -> str | None:
        """An owner, :data:`TYPE_URL`, or :data:`FINISHED` / ``None`` to stop choosing."""
        raise NotImplementedError

    def repos(self, owner: str, rows: list[Repo], ticked: set[str]) -> set[str] | None:
        """The slugs ticked in ``owner``'s list; ``None`` leaves them as they were."""
        raise NotImplementedError

    def url(self) -> str:
        raise NotImplementedError

    def one_of(self, kind: str, choices: list[str]) -> str | None:
        raise NotImplementedError


def _finished_label(selected: int) -> str:
    return f"Done ({selected} selected)"


class QuestionaryPicker(Picker):
    """Arrow keys, space to tick, type to filter, Enter to confirm."""

    def yes_no(self, question: str, default: bool = False) -> bool:
        import questionary

        answer = questionary.confirm(question, default=default).ask()
        return bool(answer) if answer is not None else default

    def owner(self, owners: list[str], selected: int, default: str | None) -> str | None:
        import questionary

        choices = [
            *[questionary.Choice(o, value=o) for o in owners],
            questionary.Choice(TYPE_URL, value=TYPE_URL),
            questionary.Choice(_finished_label(selected), value=FINISHED),
        ]
        return questionary.select(
            "Choose repositories from", choices=choices, default=default or owners[0]
        ).ask()

    def repos(self, owner: str, rows: list[Repo], ticked: set[str]) -> set[str] | None:
        import questionary

        choices = [
            questionary.Choice(row.title, value=row.slug, checked=row.slug in ticked)
            for row in rows
        ]
        picked = questionary.checkbox(
            f"Repositories in {owner}",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            instruction="(space to tick, type to filter, Enter to confirm)",
        ).ask()
        return set(picked) if picked is not None else None

    def url(self) -> str:
        import questionary

        return (questionary.text("Repository URL").ask() or "").strip()

    def one_of(self, kind: str, choices: list[str]) -> str | None:
        import questionary

        return questionary.select(f"Which {kind}?", choices=choices).ask()


class PlainPicker(Picker):
    """The same questions as numbered lists, for a terminal questionary cannot drive."""

    def __init__(self, ask: Callable[[str], str] = input, out: TextIO | None = None):
        self._ask = ask
        self._out = out

    def _say(self, line: str) -> None:
        print(line, file=self._out or sys.stdout)

    def yes_no(self, question: str, default: bool = False) -> bool:
        raw = self._ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not raw else raw in ("y", "yes")

    def owner(self, owners: list[str], selected: int, default: str | None) -> str | None:
        options = [*owners, TYPE_URL, _finished_label(selected)]
        for number, label in enumerate(options, 1):
            self._say(f"  {number}. {label}")
        raw = self._ask("Choose repositories from (number, Enter when done): ").strip()
        if not raw:
            return FINISHED
        try:
            index = int(raw) - 1
        except ValueError:
            return default
        if index == len(owners):
            return TYPE_URL
        return owners[index] if 0 <= index < len(owners) else FINISHED

    def repos(self, owner: str, rows: list[Repo], ticked: set[str]) -> set[str] | None:
        shown = rows
        query = self._ask(f"Filter {owner}'s repositories (Enter for all): ").strip().lower()
        if query:
            shown = [row for row in rows if query in row.title.lower()]
        for number, row in enumerate(shown, 1):
            mark = "x" if row.slug in ticked else " "
            self._say(f"  [{mark}] {number}. {row.title}")
        raw = self._ask("Numbers to tick, comma-separated (Enter keeps the ticks): ").strip()
        if not raw:
            return None
        chosen = set(ticked)
        for part in raw.replace(" ", ",").split(","):
            if part.isdigit() and 0 < int(part) <= len(shown):
                chosen.add(shown[int(part) - 1].slug)
        return chosen

    def url(self) -> str:
        return self._ask("Repository URL: ").strip()

    def one_of(self, kind: str, choices: list[str]) -> str | None:
        for number, choice in enumerate(choices, 1):
            self._say(f"  {number}. {choice}")
        raw = self._ask(f"Which {kind} (number)? ").strip()
        if raw.isdigit() and 0 < int(raw) <= len(choices):
            return choices[int(raw) - 1]
        return None


#: What :class:`QuestionaryPicker` imports. Step 1 imports it up front, so an
#: environment without it stops there with the sync hint, never at a prompt
#: (2026-09-24: `ModuleNotFoundError` at the first `yes_no`, after a pull).
PICKER_PACKAGES = ("questionary",)


def picker_packages_missing() -> list[str]:
    """The picker's packages that do not import from this interpreter's environment."""
    import importlib

    importlib.invalidate_caches()
    missing = []
    for name in PICKER_PACKAGES:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    return missing


def default_picker(stdin: TextIO | None = None) -> Picker:
    """Questionary on a real terminal; numbered lists on anything else."""
    stream = stdin or sys.stdin
    try:
        tty = stream.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        tty = False
    return QuestionaryPicker() if tty else PlainPicker()


# ── the steps ────────────────────────────────────────────────────────────────


def is_wsl(read: Callable[[], str] | None = None) -> bool:
    """WSL's kernel says so in /proc/version."""
    try:
        text = read() if read is not None else Path("/proc/version").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in text.lower()


def wants_device_code(env: dict[str, str] | os._Environ, platform: str) -> bool:
    """No browser to open here: over SSH, or on Linux with no display."""
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("SSH_CLIENT"):
        return True
    if platform.startswith("linux"):
        return not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    return False


def _git_install(platform: str) -> str:
    if platform == "darwin":
        return "xcode-select --install"
    return "sudo apt-get install -y git (or your distribution's package manager)"


def _gh_install(platform: str) -> str:
    if platform == "darwin":
        return "brew install gh"
    return "see https://cli.github.com for your distribution's one install command"


class Setup:
    """One run of the guided setup."""

    def __init__(
        self,
        options: Options,
        *,
        shell: Shell | None = None,
        picker: Picker | None = None,
        platform: str | None = None,
        env: dict[str, str] | None = None,
        out: TextIO | None = None,
        connect: Callable[..., dict] | None = None,
        sync_env: Callable[[], int] | None = None,
        wsl: Callable[[], bool] | None = None,
    ):
        self.options = options
        self.shell = shell or Shell()
        self._picker = picker
        self.platform = platform or sys.platform
        self.env = dict(os.environ) if env is None else env
        self.out = out or sys.stdout
        self._connect = connect
        self._sync_env = sync_env
        self._wsl = wsl or is_wsl

    @property
    def picker(self) -> Picker:
        if self._picker is None:
            self._picker = default_picker()
        return self._picker

    def say(self, line: str) -> None:
        print(line, file=self.out, flush=True)

    def run(self) -> int:
        try:
            self.machine()
            self.claude()
            self.github()
            self.papaya()
            self.repositories()
            self.profile()
        except Stop as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 1
        except EOFError:
            print(NO_TERMINAL, file=sys.stderr, flush=True)
            return 1
        self.say(DONE)
        return 0

    # 1 ── the machine

    def machine(self) -> None:
        if self.platform.startswith(("win", "cygwin", "msys")):
            raise Stop(WINDOWS_STEPS)
        if self.shell.which("git") is None:
            raise Stop(f"git is not installed. Install it: {_git_install(self.platform)}")
        if self.shell.which("uv") is None:
            raise Stop(f"uv is not installed. Install it: {UV_INSTALL}")
        from papaya_agent_runtime import envsync, readiness

        env = readiness.environment_path()
        if not readiness.environment_ready(env):
            if readiness.environment_imports(env):
                self.say("Updating the runtime's environment…")
            else:
                self.say("Building the runtime's environment…")
            code = (self._sync_env or _sync_env)()
            if code == envsync.REFUSED:
                raise Stop(
                    "The runtime's environment is out of date and a running ppy serve holds "
                    "it: run ./bin/ppy supervisor stop, then ./bin/ppy setup again"
                )
            if code != 0 or not readiness.environment_ready(env):
                raise Stop("The runtime's environment could not be built: run ./bin/ppy env sync")
        missing = picker_packages_missing() if self.options.interactive else []
        if missing:
            raise Stop(
                f"The runtime's environment is missing {', '.join(missing)}: run "
                "./bin/ppy env sync, then ./bin/ppy setup again"
            )
        where = "macOS" if self.platform == "darwin" else "Linux"
        if where == "Linux" and self._wsl():
            where = "Linux (WSL2)"
        self.say(f"✓ {where}, git, uv and the runtime's environment")

    # 2 ── Claude Code

    def _claude_signed_in(self) -> bool:
        return self.shell.capture(["claude", "auth", "status", "--text"])[0] == 0

    def claude(self) -> None:
        if self.shell.which("claude") is None:
            raise Stop(
                f"Claude Code is not installed. Install it: {CLAUDE_INSTALL}, "
                "then run ./bin/ppy setup again"
            )
        if not self._claude_signed_in():
            if not self.options.interactive:
                raise Stop("Claude Code is not signed in: run claude auth login")
            self.say("Claude Code is not signed in; starting its sign-in.")
            self.shell.attach(["claude", "auth", "login"])
            if not self._claude_signed_in():
                raise Stop("Claude Code is still not signed in: run claude auth login")
        self.say("✓ Claude Code signed in")

    # 3 ── GitHub

    def _gh_signed_in(self) -> bool:
        return self.shell.capture(["gh", "auth", "status", "--hostname", GITHUB_HOST])[0] == 0

    def github(self) -> None:
        if self.shell.which("gh") is None:
            raise Stop(
                f"The GitHub CLI (gh) is not installed. Install it: {_gh_install(self.platform)}"
            )
        if not self._gh_signed_in():
            if not self.options.interactive:
                raise Stop(f"GitHub is not signed in: run gh auth login --hostname {GITHUB_HOST}")
            self.say("GitHub is not signed in; starting gh auth login.")
            self.shell.attach(
                ["gh", "auth", "login", "--hostname", GITHUB_HOST, "--git-protocol", "https"]
            )
            if not self._gh_signed_in():
                raise Stop(
                    f"GitHub is still not signed in: run gh auth login --hostname {GITHUB_HOST}"
                )
        helper = f"credential.https://{GITHUB_HOST}.helper"
        code, out = self.shell.capture(["git", "config", "--global", "--get-all", helper])
        if code != 0 or "gh" not in out:
            code, _ = self.shell.capture(["gh", "auth", "setup-git", "--hostname", GITHUB_HOST])
            if code != 0:
                raise Stop("git could not be set to push with gh: run gh auth setup-git")
        self.say("✓ GitHub signed in")

    # 4 ── Papaya

    def papaya(self) -> None:
        from papaya_agent_runtime import papaya

        before = papaya.identity()
        if before is not None:
            self.say(f"✓ {connected_line(before)}")
            if not (self.options.interactive and not self.options.pick_repos):
                return
            if not self.picker.yes_no("Switch to another agent?", default=False):
                return
        who, result = self._connect_papaya(before=before)
        if before is None or who.agent_id != before.agent_id:
            self.say(f"✓ {connected_line(who)}")
        elif result.get("agent_choice") == "only":
            self.say(only_agent_line(who, result.get("workspace")))
        else:
            self.say(f"✓ Still connected as {agent_label(who)}")

    def _connect_papaya(self, before: Any = None, **chosen: str) -> tuple[Any, dict]:
        from papaya_agent_runtime import papaya

        device = wants_device_code(self.env, self.platform)
        if device:
            self.say("Connecting to Papaya with a device code: open the link on any device.")
        else:
            self.say("Connecting to Papaya: approve in the browser that opens.")
        kwargs: dict[str, Any] = {
            "harness": "claude",
            "workspace": chosen.get("workspace", self.options.workspace),
            "agent": chosen.get("agent", self.options.agent),
            "device": device,
            "echo": self.out,
            # On a terminal the client asks the workspace and agent itself, inside the
            # one sign-in; a script gets its choices back and stops naming the flag.
            "interactive": self.options.interactive,
        }
        result = (self._connect or papaya.connect)(**kwargs)
        if result.get("ok"):
            who = papaya.identity()
            if who is None:
                raise Stop(
                    "Papaya said connected, but no agent is pinned: run ./bin/ppy setup again"
                )
            return who, result
        reason = result.get("reason")
        if before is not None and reason != "choose":
            # A switch that did not finish leaves the old connection in place: say so,
            # rather than let the old agent read as the new one.
            why = str(result.get("detail") or reason or "").strip()
            raise Stop(
                f"Not switched: still connected as {agent_label(before)}"
                + (f" ({why})" if why else "")
                + ". Run ./bin/ppy setup again to try once more."
            )
        if reason == "choose":
            # Only when the client had no terminal to ask on (stdin piped in): the
            # re-run with the answer is a second sign-in, which a terminal never needs.
            kind = str(result.get("kind") or "agent")
            flag = str(result.get("flag") or f"--{kind}")
            choices = list(result.get("choices") or [])
            if not self.options.interactive or not choices:
                raise Stop(f"This account has more than one {kind}: pass {flag} <{kind}>")
            picked = self.picker.one_of(kind, choices)
            if not picked:
                raise Stop(f"No {kind} chosen: run ./bin/ppy setup again")
            return self._connect_papaya(before, **{**chosen, kind: picked})
        if reason == "no_installer":
            raise Stop("The Papaya client cannot be installed here: install Node or uv first")
        if reason == "timeout":
            raise Stop("Nobody approved the Papaya sign-in in time: run ./bin/ppy setup again")
        raise Stop(f"Papaya is not connected ({reason}): {result.get('detail') or ''}".rstrip())

    # 5 ── repositories

    def repositories(self) -> None:
        from papaya_agent_runtime import repos

        registered = repos.list_repos()
        if self.options.repos:
            for wanted in self.options.repos:
                self._register(wanted, registered)
            registered = repos.list_repos()
        elif registered and not self.options.pick_repos:
            pass
        elif not self.options.interactive:
            if not registered:
                raise Stop("No repository is registered: pass --repo <url>")
        else:
            self._pick(registered)
            registered = repos.list_repos()
        if not registered:
            raise Stop("At least one repository is needed: run ./bin/ppy setup again to pick one")
        n = len(registered)
        self.say(REPOS_LINE.format(n=n, noun="repository" if n == 1 else "repositories"))

    def _pick(self, registered: list[dict]) -> None:
        before = {s for s in (_slug_of(row) for row in registered) if s}
        owners = self._owners()
        ticked = set(before)
        listed: dict[str, list[Repo]] = {}
        default: str | None = None
        while True:
            choice = self.picker.owner(owners, len(ticked), default)
            if choice in (None, FINISHED):
                break
            if choice == TYPE_URL:
                url = self.picker.url()
                if url:
                    ticked.add(url)
                default = FINISHED
                continue
            rows = listed.get(choice)
            if rows is None:
                rows = listed[choice] = self._owner_repos(choice)
            slugs = {row.slug.lower() for row in rows}
            lowered = {t.lower() for t in ticked}
            shown = {row.slug for row in rows if row.slug.lower() in lowered}
            picked = self.picker.repos(choice, rows, shown)
            if picked is not None:
                ticked = {t for t in ticked if t.lower() not in slugs} | picked
            default = FINISHED
        lowered = {t.lower() for t in ticked}
        for wanted in sorted(t for t in ticked if t.lower() not in before):
            self._register(wanted, registered)
        if before - lowered:
            self.say(KEPT_LINE)

    def _gh_json(self, args: list[str]) -> Any:
        code, out = self.shell.capture(["gh", *args])
        if code != 0:
            tail = out.strip().splitlines()[-1] if out.strip() else f"exit {code}"
            raise Stop(f"Could not list your GitHub repositories (gh {args[0]}): {tail}")
        try:
            return json.loads(out) if out.strip() else []
        except ValueError as exc:
            raise Stop(
                f"gh {' '.join(args[:2])} answered with something that was not JSON"
            ) from exc

    def _owners(self) -> list[str]:
        viewer = self._gh_json(["api", "user"])
        orgs = self._gh_json(["api", "user/orgs"])
        found = [str(viewer.get("login") or "")] if isinstance(viewer, dict) else []
        if isinstance(orgs, list):
            found += [str(o.get("login") or "") for o in orgs if isinstance(o, dict)]
        owners: list[str] = []
        for owner in found:
            if owner and owner not in owners:
                owners.append(owner)
        if not owners:
            raise Stop("gh is signed in to no account with repositories: run gh auth status")
        return owners

    def _owner_repos(self, owner: str) -> list[Repo]:
        rows = self._gh_json(
            [
                "repo",
                "list",
                owner,
                "--limit",
                "1000",
                "--json",
                "nameWithOwner,description,updatedAt",
            ]
        )
        found = [
            Repo(
                slug=str(row.get("nameWithOwner") or ""),
                description=str(row.get("description") or "").strip(),
                updated_at=str(row.get("updatedAt") or ""),
            )
            for row in rows
            if isinstance(row, dict) and row.get("nameWithOwner")
        ]
        return sorted(found, key=lambda r: r.updated_at, reverse=True)

    def _register(self, wanted: str, registered: list[dict]) -> None:
        from papaya_agent_runtime import repos

        url = repo_url(wanted)
        slug = (repos.forge_slug(url) or "").lower()
        if slug and any(_slug_of(row) == slug for row in registered):
            return
        try:
            added = repos.add_repo(url)
        except repos.RepoError as exc:
            raise Stop(f"Could not register {wanted}: {exc}") from exc
        self.say(f"✓ Registered {added.name}")

    # the manager profile, silently

    def profile(self) -> None:
        from papaya_agent_runtime.config import ConfigError
        from papaya_agent_runtime.paths import config_path
        from papaya_agent_runtime.setup.wizard import run_setup

        if config_path().exists() and not self.options.profile:
            return
        try:
            run_setup(non_interactive=True, overrides=self.options.profile)
        except ConfigError as exc:
            raise Stop(f"The runtime could not be configured: {exc}") from exc
        if not self.options.skip_tools:
            from papaya_agent_runtime.paths import ensure_layout
            from papaya_agent_runtime.setup.provision import provision_all

            ensure_layout()
            failed = [r.name for r in provision_all() if r.status == "failed"]
            if failed:
                self.say(
                    f"Companion tools not installed ({', '.join(failed)}); "
                    "the runtime works without them. ./bin/ppy tools install retries."
                )


def agent_label(who: Any) -> str:
    """``<Name> (@handle)``, with whichever half is known."""
    handle = f"@{who.handle.lstrip('@')}" if getattr(who, "handle", "") else ""
    name = getattr(who, "name", "") or ""
    if name and handle:
        return f"{name} ({handle})"
    return name or handle or "an unnamed Papaya agent"


def connected_line(who: Any) -> str:
    """``Connected as <Name> (@handle)``."""
    return f"Connected as {agent_label(who)}"


def only_agent_line(who: Any, workspace: str | None) -> str:
    """Said when a switch ends on the same agent because it was the only one there."""
    return ONLY_AGENT.format(agent=agent_label(who), workspace=workspace or "this workspace")


def repo_url(wanted: str) -> str:
    """A URL for what the person named: an ``owner/name`` is a GitHub repository."""
    text = wanted.strip()
    if "://" in text or text.startswith("git@") or text.startswith(("/", ".", "~")):
        return text
    if text.count("/") == 1:
        return f"https://github.com/{text}"
    return text


def _slug_of(row: dict) -> str:
    from papaya_agent_runtime import repos

    return (repos.forge_slug(row.get("forge_url") or row.get("origin")) or "").lower()


def _sync_env() -> int:
    """What `ppy env sync` does: build this checkout's environment and swap it in."""
    from papaya_agent_runtime import envsync
    from papaya_agent_runtime.setup.provision import repo_root

    root = repo_root()
    argv = ["--project", str(root)]
    pin = root / ".python-version"
    if pin.is_file():
        argv += ["--python", pin.read_text(encoding="utf-8").strip()]
    return envsync.main(argv)


def run(options: Options, **seams: Any) -> int:
    return Setup(options, **seams).run()
