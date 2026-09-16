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
- the runtime role injected (Claude via ``--append-system-prompt``; both via the
  seed prompt), pointing at ``docs/runtime-contract.md`` as the authoritative
  contract for the session; and
- an optional objective seeded as the first message.

The manager then operates the control plane itself — the user never runs ``ppy``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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
    "configure via `ppy setup` if there's no config, and `ppy papaya connect` if this "
    "machine is not connected (their only step is clicking Approve in the browser; if "
    "they decline or it fails, carry on without Papaya and say so once — it is a "
    "preference, never a prerequisite). Then `ppy repo list`, and `ppy repo discover` "
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
    "one with `ppy papaya link`. The tasks you dispatch to get there live in your own "
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
    "`ppy task <task_id>`, `ppy status`, or a worker's `repos/<name>/tasks.md` progress log) "
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
    "plan|implement|test|review|blocked|done --note ...`; `ppy task <id>` shows the latest, "
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
) -> Launch:
    """Construct the manager harness invocation (pure; no process spawn).

    Interactive by default, which is `ppy start`. With ``turn`` — a whole prompt —
    the same manager is built for one headless turn instead (`claude -p`,
    `codex exec`): same role, same PATH, same provider resolution, and the prompt
    in place of the conversational seed. That is how `ppy serve` runs its brief,
    answer and review turns without a second, drifting harness launcher.
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

    prov, mdl, rsn = resolve_profile(config, provider, model, reasoning)
    seed = turn if turn is not None else _seed_prompt(objective, configured=config is not None)

    if prov == "claude":
        argv = ["claude"]
        if turn is not None:
            # The prompt goes straight after `-p`, not last: `--allowedTools` is
            # variadic, and a positional after it can be read as one more tool.
            argv += ["-p", seed]
        if mdl:
            argv += ["--model", mdl]
        argv += ["--append-system-prompt", _ROLE]
        # Let the manager drive the control plane without a prompt per call; `ppy`
        # itself is the authority gate, so allowing the wrapper is safe.
        argv += ["--allowedTools", "Bash(ppy:*)", "Bash(./bin/ppy:*)"]
        if turn is None:
            argv += [seed]
    else:  # codex
        argv = ["codex"]
        if turn is not None:
            argv += ["exec"]
        if mdl:
            argv += ["--model", mdl]
        if rsn:
            argv += ["-c", f"model_reasoning_effort={rsn}"]
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


def run_turn(launch: Launch, *, should_stop=None) -> TurnResult:
    """Run a headless turn built by :func:`build_launch` to completion. Blocking.

    The counterpart of :func:`start` for a turn: `start` replaces this process
    with an interactive manager, and this runs one alongside it and waits. Output
    is captured whole, because the next attempt at a turn that missed its job is
    given the previous transcript's tail. ``should_stop`` is polled while the
    turn runs; when it answers true the harness is terminated, so a ticket that is
    handed back does not leave a manager working on it.
    """
    import subprocess
    import tempfile

    from papaya_agent_runtime.paths import ensure_layout

    ensure_layout()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
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
