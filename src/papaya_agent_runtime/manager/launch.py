"""Build and start an interactive manager session (optional explicit launcher).

The primary front door is simply opening a harness in the repo: this repo's
``CLAUDE.md``/``AGENTS.md`` route it into the runtime manager, which self-bootstraps
via its preflight. ``ppy start`` remains available as an explicit launcher (handy
from any cwd or to force a provider) — it does the same thing more directly.

``build_launch`` is a pure function (argv + env + cwd) so it is fully testable;
``start`` performs the actual process handoff. The manager harness runs
interactively in the Papaya Agent Runtime repository root with:

- the repository ``bin/`` and provisioned ``.ppy/tools/bin`` prepended to PATH, so
  the manager calls ``ppy`` and companions directly;
- the runtime role injected, pointing at ``docs/runtime-contract.md`` as the
  authoritative contract for the session: Claude carries it as an appended system
  prompt, interactive and headless alike; Codex has no such seam, so an interactive
  session reads it through the seed prompt and a headless turn gets it prepended to
  the turn prompt (:func:`codex_turn_prompt`); and
- an optional objective seeded as the first message.

The manager then operates the control plane itself — the user never runs ``ppy``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import papaya_agent_runtime
from papaya_agent_runtime.config import MMConfig

RUNTIME_CONTRACT = "docs/runtime-contract.md"

_ROLE = (
    "You are a Papaya agent operating at RUNTIME for the user (not developing the "
    "Papaya Agent Runtime codebase). The person speaking to you is the user. Read and "
    f"follow {RUNTIME_CONTRACT} as the authoritative contract for this session; it "
    "overrides any repository-development instructions in AGENTS.md/CLAUDE.md. Core "
    "rule: never ask the user to run a command — you operate the control plane "
    "yourself via your shell (invoke it as `./bin/ppy`) for bootstrapping, "
    "configuration, delegation, review, and delivery, and speak to the user only in "
    "natural language. "
    "IDENTITY: you have no persona of your own — you are whichever Papaya agent this "
    "machine is connected as. `./bin/ppy papaya status` says who that is, and the "
    "harness loads that agent's persona, rules and memories; treat them as standing "
    "instructions and let the workspace's rules win where they touch this prompt. "
    "PREFLIGHT: before your first reply, silently get the house in order — run "
    "`./bin/ppy doctor`, run `./bin/install` yourself if the environment isn't ready, "
    "configure via `ppy setup` if there's no config. If this machine is not connected "
    "to Papaya, run standalone: everything local works the same, do not start a "
    "sign-in or wait for one, and do not repeat the one-line invitation already "
    "printed at launch (connect only if the user asks). Then `ppy repo list`, and "
    "`ppy repo discover` "
    "when nothing is registered, so your first reply has something concrete to offer. "
    "Only speak up if a prerequisite you truly cannot fix is missing (uv, git, Node, "
    "gh, or a signed-in harness); otherwise preflight is invisible. The user never runs "
    "install/setup/a launch step — that's your job. "
    "VOICE: direct, useful, warm, and on the user's side — a teammate who is good at "
    "this and wants the work to come out well. Lead with the answer or the outcome; no "
    "preamble, no 'great question', no restating the ask. NEVER fabricate a path, "
    "result, metric, URL, id, or capability: a claim about their code or workspace "
    "needs evidence from a tool or the conversation, and if you don't have it, say so "
    "or go get it. Be proactive, not chatty — notice the adjacent thing that matters "
    "(the test that will break, the repo worth registering, the PR that's been red for "
    "an hour) and offer it in one line. Ask for exactly what's missing and stop. Own "
    "the work to done; when something breaks, say what broke and what you're doing, "
    "without apology paragraphs. Partial success is success: finish the rest and name "
    "what you skipped. No emojis or decorative unicode. "
    "RESULTS, NOT NARRATION (the user cares about this most): they do not care how you "
    "do the job. Never narrate mechanics ('running the bootstrap now', 'let me run ppy "
    "doctor'), never read internal state back like an audit (config/`.ppy` paths, "
    "commit SHAs, DB schema, capability matrix, versions, `ppy` subcommands, raw command "
    "output, tables of plumbing), and never hand the user a command to run. Do the work "
    "silently and report the outcome in one tight line. Answer the exact question asked "
    "and nothing more. Surface a detail only if it changes what the user should think "
    "or do (a blocker, a real decision, a risk, cost). Get shorter and more exact when "
    "things are urgent or broken. "
    "REPOSITORIES: work only ever happens in repos registered under `.ppy/repos/`, but "
    "go and get them — `ppy repo discover` reads the forge (their account and orgs) and "
    "offers what is not registered yet; never scan the filesystem or guess a path. A "
    "repo the user names that you don't have is never a dead end: find it, offer it, "
    "register it on their word. Onboard every repo the moment it is registered with "
    "`ppy repo onboard <name>`, so the first dispatch knows the test command and the CI "
    "gate instead of guessing. "
    "DEFINE DONE BEFORE YOU START: a tracked record you pick up that carries no "
    "acceptance criteria or validation steps gets them FIRST, written onto the record "
    "itself, so the person who asked can correct them before the work exists and the "
    "next reader can tell whether it is finished. If a criterion is not yours to decide, "
    "write what you can, name the gap and ask. One question before the work beats a "
    "rejected pull request after it. "
    "PAPAYA TRACKS WORK, NOT STEPS: a work item is for something a person would look "
    "for later (a feature, a bug worth a record, a proposal, something awaiting "
    "sign-off). Keep the items that exist current, and link the tasks that belong to "
    "one with `ppy track <task_id> --record <id> --url <url> --title <title>`. The "
    "tasks you dispatch to get there live in your own "
    "ledger — minting a work item per task is noise. Reply in an item's existing "
    "thread with its URL, and name people by @handle. "
    "DESCRIBE THINGS, NEVER CITE LABELS: a plan's '1B', a spec's '§5', a 'rev3' mean "
    "nothing to someone not holding that document. Say what the thing is ('the plan "
    "step that adds per-item summaries and links — the second backend PR'); a label may "
    "follow once in parentheses. The turn-end hook hands a label-laden reply back to you "
    "once, naming the labels, before it reaches the user. "
    "USE SKILLS/SCRIPTS, DON'T IMPROVISE: procedure and plumbing live in skills "
    "(judgment) and scripts/`ppy` (mechanics), not in this prompt. When a task matches a "
    "skill (e.g. `setup-runtime` for install/config, `review-surfaces` for visual "
    "review + feedback), load and follow it instead of hand-rolling shell. "
    "NEVER BLOCK — the only thing you ever block on is producing your reply to the user. "
    "Workers run in the background supervisor daemon and stream their status to state on "
    "their own; a review surface is a hand-off (the `review-surfaces` script backgrounds "
    "it). To learn what's happening, take a NON-BLOCKING snapshot (`ppy run <run_id>`, "
    "`ppy task show <task_id>`, `ppy status`, or a worker's `repos/<name>/tasks.md` progress log) "
    "and hand the turn back — do NOT sit in a blocking `ppy wait` during a live turn (it "
    "freezes the conversation). `ppy wait` is a scripting primitive; if ever used live, only "
    "as a non-blocking drain (`--timeout 0`). Pick actionable results up on a later turn. "
    "SELF-HEAL & IMPROVE (see the `self-improvement` skill): proactively repair breakage "
    "(drift, dead supervisor, missing companions, stuck runners) with ppy's own recovery "
    "and get sharper as you go (reuse durable decisions, right-size the worker, cut "
    "redundant work) — but strictly within your authority: never edit this framework's "
    "source/contract/config to optimize, and never weaken ceilings, the review gate, or "
    "scope for speed. Complete periodic evidence-backed assessments when `ppy` surfaces "
    "them, propose 1-3 measurable experiments, and align the plan with the user. Propose "
    "structural changes; don't self-apply them. "
    "TRACK YOUR WORK IN THE LEDGER, NOT IN YOUR HEAD: your intent lives in `ppy todo` — "
    '`ppy todo add "..." [--run <id>] [--task <id>] [--blocked-on user|review|task:<id>]`, '
    "`ppy todo done <id>`, `ppy todo list`. Record a next step the moment you know it and "
    "close it when it lands; with work open and no todo recorded, `ppy` will refuse to let "
    "you stop until you write one. `ppy board` renders the work board (`.ppy/memory/tasks.md` "
    "is generated from it — never hand-edit). "
    "WATCH THE TEAM THROUGH STATE: workers report `ppy progress <task_id> --phase "
    "plan|implement|test|review|blocked|done --note ...`; `ppy task show <id>` shows the latest, "
    "`ppy memory show --repo <name>` the log. Workers post their PLAN first — read it early "
    "and, if the approach is wrong, COURSE-CORRECT with `ppy steer <task_id> --message ...` "
    "(or `ppy resume`) rather than waiting to reject the finished diff. The supervisor "
    "flags a worker that never posted a plan (`plan_missing`) or has gone silent "
    "(`worker_quiet`) as actionable events via `ppy run`/`ppy wait`; `ppy health` on demand. "
    "Treat them like any actionable event — peek, then steer, resume, or reconcile; don't "
    "let a stuck worker sit. "
    "DURABLE MEMORY (see the `durable-memory` skill): machine-local memory under "
    "`.ppy/memory/` — per-instance (`preferences.md`, `relationships.md`, "
    "`improvements.md`) is YOUR layer; per-repo `repos/<name>/notes.md` + `tasks.md` "
    "(follow-ups) is the shared WORKER workspace — so you never relearn a repo, a "
    "relationship, or the user's preferences. Read it at session start; record durable "
    "facts as you learn them; check memory before re-asking. Live state stays in "
    "`.ppy/state.db`; memory is what should outlive the run. "
    "HANDOFF (see the `handoff` skill): when the user asks to hand off, wrap up, save "
    "state, or pause — or before context is compacted — make the ledger true (`ppy todo`), "
    "then run `ppy handoff` and reply with the pickup prompt it prints, verbatim in a code "
    "block, with its warnings about in-flight workers called out above it in plain words. "
    "The snapshot itself goes to `.ppy/memory/handoff.md`; the short prompt naming it is "
    "the deliverable. If you are STARTED with a pickup prompt (or a session-start pickup "
    "context appears), reconnect to the team first (`ppy supervisor status`, "
    "`ppy reconcile`, `ppy health`), read the snapshot, and reconcile it against live state "
    "before speaking. "
    "SCOPE (hard boundary): the ONLY repositories that exist are the ones registered "
    "with Papaya Agent Runtime (`ppy repo list`, materialized under `.ppy/repos/`). Never look "
    "outside `.ppy/repos/` — do not scan `~`, `~/workspace`, or the machine for repos, "
    "and never guess local paths like `~/workspace/foo`. Resolve any repo the user "
    "names against `ppy repo list`; if it isn't registered, ask for its URL/path and "
    "`ppy repo add` it rather than hunting for it. Also ignore this framework's own "
    "AGENTS.md/CLAUDE.md/docs — they are for engineers building Papaya Agent Runtime, not you; "
    "do not read them or treat this session as developing the framework. Nothing on the "
    "machine outside `.ppy/` is in scope."
)


class ManagerLaunchError(Exception):
    pass


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    provider: str
    model: str | None = None
    reasoning: str | None = None
    seed_prompt: str = ""
    extra: dict = field(default_factory=dict)


def repo_root() -> str:
    """The Papaya Agent Runtime repository root (parent of the installed package)."""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(papaya_agent_runtime.__file__)))
    )


def _seed_prompt(objective: str | None, configured: bool) -> str:
    lines = [
        f"Starting a Papaya Agent Runtime session. Read and follow {RUNTIME_CONTRACT} now, "
        "including its Preflight and Voice & tone rules — have a real personality and "
        "speak in outcomes, not mechanics.",
    ]
    if configured:
        lines.append(
            "Run your preflight silently (bootstrap/install/setup/connect yourself as "
            "needed; do not report diagnostics, setup summaries, paths, versions, or "
            "command output back to me). Then open with one line saying you're ready "
            "and who you're connected as, and either the state of what's already in "
            "flight or — if nothing is registered — the repos worth taking on."
        )
    else:
        lines.append(
            "Papaya Agent Runtime isn't configured yet. Sort it out yourself via preflight: "
            "run `./bin/install` if the environment needs it, then walk me through "
            "configuration in plain conversation and write it with `ppy setup`. Don't "
            "ask me to run commands, and don't read settings back — just confirm we're "
            "good."
        )
    if objective:
        lines.append(f"My objective: {objective}")
    else:
        lines.append("Then ask me what I'd like to accomplish.")
    return " ".join(lines)


def resolve_profile(
    config: MMConfig | None,
    provider: str | None,
    model: str | None,
    reasoning: str | None,
) -> tuple[str, str | None, str | None]:
    """Resolve (provider, model, reasoning) for the manager session.

    Explicit overrides win. Otherwise fall back to the configured manager
    profile. ``model``/``reasoning`` fall back to the config only when the
    resolved provider matches the configured manager provider (so an override to
    the *other* provider does not inherit a mismatched model name).
    """
    resolved_provider = provider or (config.manager.provider if config else None)
    if resolved_provider is None:
        raise ManagerLaunchError(
            "no manager provider: pass --provider claude|codex or run setup first"
        )
    if resolved_provider not in ("claude", "codex"):
        raise ManagerLaunchError(f"unknown provider {resolved_provider!r}")

    same_as_config = bool(config and config.manager.provider == resolved_provider)
    resolved_model = model or (config.manager.model if same_as_config else None)
    resolved_reasoning = reasoning or (config.manager.reasoning if same_as_config else None)
    return resolved_provider, resolved_model, resolved_reasoning


#: The job environment the Papaya client gives every job, as far as a turn's
#: tools are concerned. The client's bundled runners read the same names.
AGENT_BIN_ENV = "PAPAYA_AGENT_BIN"
AGENT_REF_ENV = "PAPAYA_AGENT_REF"
PLUGIN_DIR_ENV = "PAPAYA_PLUGIN_DIR"
#: The bundled Claude runner's override for its permission mode, honoured here too.
PERMISSION_MODE_ENV = "PAPAYA_CLAUDE_PERMISSION_MODE"
DEFAULT_PERMISSION_MODE = "bypassPermissions"

#: The Papaya MCP server's tools, by the name `runner-config` gives the server.
PAPAYA_MCP_TOOLS = "mcp__papaya__*"


@dataclass(frozen=True)
class TurnTools:
    """What a headless turn loads to act as the Papaya agent, not just as a shell.

    The first real run (2026-09-16, PAP-217) launched turns with nothing but a
    `ppy` allowlist: no MCP server, no plugin. In `-p` mode every MCP call was
    then refused, so the brief turn could not read the item's comments, write
    acceptance criteria or say anything on it. This is the shape the client's
    bundled runners (`papaya-claude-runner.sh`, `papaya-codex-runner.sh`) give a
    job, built from the same `papaya-agent mcp runner-config` output.
    """

    #: Claude: the JSON file `runner-config` wrote, for `--mcp-config`.
    mcp_config: str | None = None
    #: Codex: one `-c` value per line of `runner-config` output.
    codex_overrides: tuple[str, ...] = ()
    #: The client's bundled plugin, which carries the write-boundary hook.
    plugin_dir: str | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE


def papaya_agent_command(env: dict[str, str]) -> list[str]:
    """How a turn calls back into the Papaya client this process runs.

    `$PAPAYA_AGENT_BIN` when the job names one. The client only sets it when it
    was itself started as the `papaya-agent` console script, which `ppy serve`
    is not, so otherwise the console script installed beside this interpreter —
    the same release the listener embeds, rather than whichever `papaya-agent`
    happens to be first on PATH — and failing that, the module itself.
    """
    explicit = str(env.get(AGENT_BIN_ENV) or "").strip()
    if explicit:
        return [explicit]
    beside = Path(sys.executable).parent / "papaya-agent"
    if beside.is_file() and os.access(beside, os.X_OK):
        return [str(beside)]
    return [sys.executable, "-m", "papaya_agent_client"]


def prepare_turn_tools(
    provider: str,
    env: dict[str, str],
    *,
    root: str,
    config_file: Path,
    run=None,
) -> TurnTools:
    """Ask the client for this job's MCP configuration, the way its runners do.

    Raises :class:`ManagerLaunchError` when the client refuses: a turn launched
    without its tools is the silent failure this exists to end, so it is not
    launched at all. ``run`` is `subprocess.run`'s seam.
    """
    import subprocess

    runner = run or subprocess.run
    harness = "claude-code" if provider == "claude" else "codex"
    command = [*papaya_agent_command(env), "mcp", "runner-config", "--harness", harness]
    agent_ref = str(env.get(AGENT_REF_ENV) or "").strip()
    if agent_ref:
        command += ["--agent", agent_ref]
    command += ["--working-directory", root]
    try:
        proc = runner(
            command, env=env, cwd=root, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagerLaunchError(f"could not ask the Papaya client for MCP config: {exc}") from exc
    if proc.returncode != 0:
        said = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = said[-1] if said else "no output"
        raise ManagerLaunchError(
            f"`papaya-agent mcp runner-config` exited {proc.returncode}: {reason}"
        )
    if provider != "claude":
        overrides = tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())
        return TurnTools(codex_overrides=overrides)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(proc.stdout, encoding="utf-8")
    plugin_dir = str(env.get(PLUGIN_DIR_ENV) or "").strip()
    mode = str(env.get(PERMISSION_MODE_ENV) or "").strip() or DEFAULT_PERMISSION_MODE
    return TurnTools(
        mcp_config=str(config_file),
        plugin_dir=plugin_dir if plugin_dir and Path(plugin_dir).is_dir() else None,
        permission_mode=mode,
    )


#: Set in a headless manager turn's environment (never in an interactive session).
MANAGER_TURN_ENV = "PPY_MANAGER_TURN"

#: What separates the runtime role from the turn prompt when both travel as one
#: Codex prompt, so the turn's own heading is still the first line after it.
_ROLE_SEPARATOR = "\n\n---\n\n"


def codex_turn_prompt(turn: str) -> str:
    """The runtime role, then the turn: one prompt, because `codex exec` has no system-prompt seam.

    The same :data:`_ROLE` Claude gets through ``--append-system-prompt``, so a headless
    turn carries the contract whichever provider drives it.
    """
    return _ROLE + _ROLE_SEPARATOR + turn


def build_launch(
    *,
    config: MMConfig | None,
    provider: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    objective: str | None = None,
    root: str | None = None,
    base_env: dict[str, str] | None = None,
    turn: str | None = None,
    tools: TurnTools | None = None,
) -> Launch:
    """Construct the manager harness invocation (pure; no process spawn).

    Interactive by default, which is `ppy start`. With ``turn`` — a whole prompt —
    the same manager is built for one headless turn instead (`claude -p`,
    `codex exec`): same role, same PATH, same provider resolution, and the prompt
    in place of the conversational seed. That is how `ppy serve` runs its brief,
    answer and review turns without a second, drifting harness launcher.

    ``tools`` (from :func:`prepare_turn_tools`) is what makes a turn the Papaya
    agent: the pinned MCP server with every other one shut out, and the client's
    plugin. An interactive session loads the person's own configuration instead.
    """
    root = root or repo_root()
    env = dict(base_env if base_env is not None else os.environ)
    bin_dir = os.path.join(root, "bin")
    # Prepend both the repo `bin/` (so the manager calls `ppy`) and the provisioned
    # companion bin (so `lavish-axi` et al. resolve to the managed copy).
    from papaya_agent_runtime.paths import tools_bin_dir

    prefixes = [bin_dir, str(tools_bin_dir())]
    env["PATH"] = os.pathsep.join([*prefixes, env.get("PATH", "")])
    env["PPY_MANAGER_SESSION"] = "1"
    if turn is not None:
        # A headless `ppy serve` turn owns one ticket; the session hooks hold an
        # interactive manager to everything owed, and a turn only to its own work.
        env[MANAGER_TURN_ENV] = "1"
    else:
        env.pop(MANAGER_TURN_ENV, None)

    prov, mdl, rsn = resolve_profile(config, provider, model, reasoning)
    seed = turn if turn is not None else _seed_prompt(objective, configured=config is not None)
    if prov == "codex" and turn is not None:
        # Codex has no system-prompt flag. Until 2026-09-17 a Codex turn was launched
        # with the bare turn prompt, so the contract reached it only if Codex followed
        # `AGENTS.md` (whose first line tells a runtime session to stop reading it) two
        # hops to the contract; Claude had it in its system prompt the whole time (#74).
        seed = codex_turn_prompt(turn)

    if prov == "claude":
        argv = ["claude"]
        if turn is not None:
            # The prompt goes straight after `-p`, not last: `--allowedTools` is
            # variadic, and a positional after it can be read as one more tool.
            argv += ["-p", seed]
        if mdl:
            argv += ["--model", mdl]
        argv += ["--append-system-prompt", _ROLE]
        allowed = ["Bash(ppy:*)", "Bash(./bin/ppy:*)"]
        if turn is not None and tools is not None and tools.mcp_config:
            # The bundled Claude runner's flags. `--strict-mcp-config` is what keeps
            # a `papaya` server some earlier `connect` left in the person's config —
            # possibly another agent — from loading beside this job's own.
            argv += ["--enable-auto-mode", "--strict-mcp-config"]
            argv += ["--mcp-config", tools.mcp_config]
            argv += ["--permission-mode", tools.permission_mode]
            if tools.plugin_dir:
                argv += ["--plugin-dir", tools.plugin_dir]
            allowed.append(PAPAYA_MCP_TOOLS)
        # Let the manager drive the control plane without a prompt per call; `ppy`
        # itself is the authority gate, so allowing the wrapper is safe. Listed
        # even under `bypassPermissions`, so a stricter mode still runs a turn.
        argv += ["--allowedTools", *allowed]
        if turn is None:
            argv += [seed]
    else:  # codex
        argv = ["codex"]
        if turn is not None:
            argv += ["exec"]
            if tools is not None:
                # Codex has no strict-config flag: `runner-config` prints `-c` values
                # that switch every other server off and add this job's own.
                for override in tools.codex_overrides:
                    argv += ["-c", override]
        if mdl:
            argv += ["--model", mdl]
        if rsn:
            argv += ["-c", f"model_reasoning_effort={rsn}"]
        if turn is not None:
            argv += ["--cd", root]
        argv += [seed]

    return Launch(
        argv=argv,
        env=env,
        cwd=root,
        provider=prov,
        model=mdl,
        reasoning=rsn,
        seed_prompt=seed,
    )


@dataclass(frozen=True)
class TurnResult:
    """How one headless manager turn ended, and what it said."""

    exit_code: int
    transcript: str
    #: True when the turn was ended from outside (the ticket's hold stopped).
    stopped: bool = False

    def tail(self, chars: int = 4000) -> str:
        """The end of the transcript, which is where a turn says what it did last."""
        return self.transcript[-chars:]


#: How often a running turn checks whether it has been told to stop.
TURN_STOP_POLL_SECONDS = 1.0


def run_turn(launch: Launch, *, should_stop=None, transcript_path=None) -> TurnResult:
    """Run a headless turn built by :func:`build_launch` to completion. Blocking.

    The counterpart of :func:`start` for a turn: `start` replaces this process
    with an interactive manager, and this runs one alongside it and waits. Output
    is captured whole, because the next attempt at a turn that missed its job is
    given the previous transcript's tail. ``should_stop`` is polled while the
    turn runs; when it answers true the harness is terminated, so a ticket that is
    handed back does not leave a manager working on it.

    With ``transcript_path`` stdout and stderr are written there as the turn runs,
    and kept: a person, or a later turn, can read what a turn did (and watch one
    that is still running). Without it the output lives only as long as the call.
    """
    import subprocess
    import tempfile

    from papaya_agent_runtime.paths import ensure_layout

    ensure_layout()
    if transcript_path is not None:
        Path(transcript_path).parent.mkdir(parents=True, exist_ok=True)
        opened = open(transcript_path, "w+", encoding="utf-8")  # noqa: SIM115 - closed below
    else:
        opened = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115 - closed below
    with opened as output:
        try:
            proc = subprocess.Popen(
                launch.argv,
                cwd=launch.cwd,
                env=launch.env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ManagerLaunchError(
                f"{launch.provider} CLI not found on PATH; install it and sign in "
                "(`ppy doctor` shows harness status)"
            ) from exc
        from papaya_agent_runtime.supervisor import lifeline

        # A turn shares `serve`'s process group; the lifeline ends it if serve dies abruptly.
        lifeline.watch_pid(proc.pid)
        stopped = False
        while True:
            try:
                code = proc.wait(timeout=TURN_STOP_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if should_stop is not None and should_stop():
                    stopped = True
                    proc.terminate()
                    try:
                        code = proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        code = proc.wait()
                    break
        lifeline.release_pid(proc.pid)
        output.seek(0)
        return TurnResult(exit_code=int(code), transcript=output.read(), stopped=stopped)


def start(launch: Launch) -> None:
    """Hand off to the harness, replacing the current process (does not return)."""
    from papaya_agent_runtime.paths import ensure_layout

    ensure_layout()
    os.chdir(launch.cwd)
    try:
        os.execvpe(launch.argv[0], launch.argv, launch.env)
    except FileNotFoundError as exc:
        raise ManagerLaunchError(
            f"{launch.provider} CLI not found on PATH; install it and sign in "
            "(`ppy doctor` shows harness status)"
        ) from exc
