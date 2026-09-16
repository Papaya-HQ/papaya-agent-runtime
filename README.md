# Papaya Agent Runtime

![Papaya Agent Runtime](docs/PapayaAgentRuntime.png)

**The home you point a Papaya connected agent at when you want it to build code.**

Open Claude Code or Codex in this repository and start talking. The session becomes
your Papaya agent — its name, role, rules and memories, all of it — with a full
engineering control plane underneath: isolated worktrees, parallel workers across
every repository you own, an exact-HEAD review gate, and pull requests that arrive
already reviewed.

Two things make it different from a coding agent in a terminal:

- **It's a member of your workspace, not a tool beside it.** It knows who it is,
  what your team is working on, and which work items are waiting. It keeps the ones
  that matter current and links the code it ships back to them.
- **It goes and gets the work.** It finds the repositories you actually have, offers
  to take them on, learns how each one builds and tests before the first dispatch,
  and tells the workspace what it can do.

## How it works

- **You state intent.** Plain language, one conversation. "Add a health endpoint to
  the API and open a PR."
- **It decomposes and delegates.** Bounded tasks, each in its own isolated worktree,
  routed to the *cheapest worker that can actually do the job* — with a hard,
  code-enforced ceiling you set. Mixed providers are fine: Claude driving, Codex on
  the workers, or the reverse.
- **It reviews before you do.** It answers routine worker questions from durable
  decisions, reads the exact diff that will be pushed, and comes back with a pull
  request that has already been looked at — interrupting you only for the calls that
  need a person with authority.

The contract it runs on is [`docs/runtime-contract.md`](docs/runtime-contract.md).

## Identity comes from Papaya

The runtime has **no persona of its own**. Connecting pins this machine to one of
your workspace's agents — either from the Papaya desktop app or with
`papaya-agent connect` in a terminal; the runtime finds either one. From then on
the session *is* that agent:
persona, objective, rules and memories are loaded from Papaya and treated as
standing instructions. `ppy papaya status` says who you're connected as.

Preflight runs the connect flow itself — your only step is clicking Approve in the
browser. If you decline, or Papaya is unreachable, everything else still works:
repositories, workers, reviews and pull requests are all local. You lose the
workspace, not the ability to ship.

## Repositories: it comes to you with options

- **`ppy repo discover`** reads the forge — your account and every organization you
  belong to — and offers what isn't registered yet, most recently pushed first.
  Archived repositories never appear; forks are skipped unless you ask. It only ever
  offers: registering is an explicit act, because that's what makes a repository
  something a worker may change.
- **Naming a repo it doesn't have is never a dead end.** It finds it, offers it, and
  registers it on your word.
- **`ppy repo onboard`** reads a registered repository properly — how it builds, how
  it tests, the commands its CI workflows *actually* run, which contracts it carries
  for agents, whether UI work has a design reference to match — and writes that into
  the repository's durable notes, where the next brief and the next worker both read
  it. It names what it couldn't determine, so the unknown gets asked about instead of
  guessed at.

Work itself only ever happens inside repositories registered under `.ppy/repos/`. It
never scans your filesystem.

## Papaya tracks work, not steps

A tracked record is for something a person would look for later: a feature, a bug
worth a record, a proposal, something awaiting sign-off. The tasks dispatched to get
there live in the runtime's own ledger. Minting a ticket per task is noise, and the
runtime is built not to. What it does do is keep the records that exist current, and
link the tasks that belong to one (`ppy track`) so the pull request body names the
work it was for and where to find it.

**Papaya work items are the default, not the assumption.** A workspace that tracks
work in Linear, Notion or Jira has said so, and that wins — the agent reads it from
the workspace's own durable context and from whatever providers are connected, so a
new machine honours it on its first turn with nothing to configure. The runtime never
talks to a tracker itself; it records which record a task belongs to and renders it.

## What works today

- **Your harness is the front door.** No launch step, no setup homework — it
  bootstraps itself.
- **Real Claude & Codex workers, on a leash.** Smallest-eligible-worker routing with
  a hard, code-enforced cost ceiling.
- **Parallel, and never blocking.** Workers run in a background supervisor daemon,
  each in its own worktree, across the same repo *and* across repos. Status is a live
  snapshot, never a frozen prompt.
- **Steering that works.** Interrupt and resume, validated live against both CLIs
  ([`docs/provider-capabilities.md`](docs/provider-capabilities.md)).
- **Autonomous completion.** Durable scoped decisions, automatic question routing, an
  exact-HEAD review gate, and PR delivery via `gh`/`gh-axi`.
- **It doesn't lose things.** SQLite source of truth, append-only event logs, and
  restart / replay / reconcile recovery.
- **It repairs itself.** Drift, a stalled runner, a missing companion — fixed in
  passing. It will not rewire its own guardrails or loosen a ceiling to save time;
  those are enforced in code, not prompts.
- **It reviews its own performance.** Evidence is collected while it works, and it
  brings a short, measurable improvement plan without being asked. Structural changes
  stay proposals until you approve them.
- **It remembers, at two levels.** Durable notes under `.ppy/memory/`: an *instance*
  tier spanning all your repos (your preferences, how the repos relate, the work
  board, aligned experiments) and a *per-repo* tier (`repos/<name>/` — build and test
  incantations, conventions, gotchas, and a progress log). The per-repo files double
  as a back channel: workers post their plan up front, so a wrong turn is caught
  while it's still cheap.
- **Cross-repo aware.** Multi-repo work is one run of dependency-linked tasks, with a
  computable rollout order (`ppy plan`).
- **Pinned, verified companions.** treehouse (worktrees), lavish-axi (rich review),
  gh-axi (delivery) — downloaded, checksum-verified, pinned at setup.

## Prerequisites

It installs what it can, but these have to exist:

- **macOS or Linux.** Windows is out of scope for v1.
- **Python 3.13+**, **Git**, and **[uv](https://docs.astral.sh/uv/)**.
- **Node 22+** and the **`gh` CLI** — for companions, discovery, and PR delivery.
- **A signed-in Claude Code and/or Codex CLI.** It never logs you in. One is enough;
  two lets you run one as the driver and cap workers at the other.

A missing prerequisite is the one thing preflight will stop and tell you about.

## Getting started

```bash
git clone https://github.com/Papaya-HQ/papaya-agent-runtime
cd papaya-agent-runtime

claude          # or: codex
```

That's the whole procedure. On the first turn it runs a quiet preflight: checks the
environment, runs `./bin/install` itself if needed, connects to Papaya (you click
Approve), configures itself in conversation if it has never been set up, provisions
the pinned companions, and looks at what repositories you have. You never type
`install`, `setup`, or a launch command.

### What the first turn looks like

```text
you  ▸ claude
ppy  ◂ Ready, connected as @engineering_agent. Nothing registered yet — the
       frontend monorepo, the API and the infra repo all look active. Want me to
       take any of those on?
you  ▸ the API, and add a /healthz endpoint there
ppy  ◂ Registered and onboarded: Python, uv, pytest, CI runs ruff + pytest. Worker
       is on the endpoint now.
ppy  ◂ PR up: returns 200, JSON body, no auth. I read the diff. CI green. One thing
       worth knowing — there's no smoke test covering it. Want one?
```

> `./bin/ppy start` does the same thing more directly — from any directory, and it
> can force a provider: `./bin/ppy start --provider claude "add a health endpoint"`.
> Optional. Opening your harness is enough.

### Clone, point, connect

The other front door is the Papaya desktop app, and it has no setup step at all:
clone this repository, point the app's working folder at it, and connect. That's
the procedure. The client execs `ppy serve`, and `serve` **configures the checkout
itself** before it starts listening — the `.ppy` layout, the state database and the
memory tree are created and the config is written, with the driver *and* the
workers on the harness you chose when you connected. (Both on the same one: the
runtime doesn't mix agents behind your back. `ppy config models` changes either.)

Anything left that needs *you* — signing a harness in, most often — arrives once as
a direct message from your agent, naming what closes it. Once, not on every
restart: an unchanged situation stays quiet, and a new one speaks however soon it
appears. Having no repositories registered isn't one of those things and never
stops it working: a work item that names a repository registers it on pick-up, and
`ppy repo add` is for getting ahead of that.

### The control plane, if you want to look

You don't type these — the runtime does — but nothing is hidden:

```bash
ppy capabilities --json                 # what this runtime is, for the client that found it
ppy doctor                              # environment, capability drift, Papaya connection
ppy papaya status                       # which Papaya agent this machine is
ppy papaya connect                      # sign in and pin this machine to an agent
ppy repo discover                       # repos on the forge that aren't registered yet
ppy repo add https://github.com/you/your-repo
ppy repo onboard your-repo              # learn its build, tests, CI gate, conventions
ppy serve                               # the always-on manager: supervisor + Papaya loop
ppy supervisor serve                    # per-task runners, durable state
ppy dispatch --repo your-repo --brief brief.md --provider claude
ppy worktree list                       # every leased slot: task, state, size
ppy review show <task_id> && ppy review approve <task_id>
ppy deliver <task_id>                   # refused unless approved at current head
ppy track <task_id> --record ENG-1183 --provider linear --url ... --title "..."
ppy answer <task_id> --answer "use /v2/health" --scope run   # recorded + reused
ppy plan <run_id>                       # cross-repo rollout order
ppy usage <run_id>                      # model token accounting
ppy assessment status                   # performance-review cadence
```

`ppy capabilities --json` is the one the Papaya client reads rather than types. When
you connect a machine, the client finds this checkout and has to know what it can
delegate here; this prints one JSON object — `runtime`, `version`, `client_version`
(the `papaya-agent-client` release embedded in this checkout), `protocol` (the
supervised-protocol version that client speaks) and `modes` (the launch modes this
runtime can serve — today `supervised` and `terminal`, both of them `ppy serve`) —
from local state only, so it answers instantly, offline, and on a machine that has
never been set up — including one where `uv sync` has never run. `bin/ppy` answers
this one command from the standard library when the project environment does not
exist yet, so the client's ten-second probe never waits on a first-time install of
sixty-odd packages. It never fails: a checkout where the client cannot be imported
reports `client_version: null` and still exits 0. `ppy doctor` and `ppy readiness`
show the same embedded client version, and readiness warns — without blocking — when
the client that launched this runtime is newer than the one in the checkout.

## Running as the Papaya manager

`ppy serve` is the always-on manager. It runs until told to stop, and in one process
it runs this runtime's own supervisor — the same one `ppy supervisor serve` runs, so
`ppy dispatch`, `ppy review` and `ppy deliver` from any shell on the machine reach it
— and the Papaya client's event loop **in-process**, through the client's library
entry points. There is no copy of the client's cursor, reservation, renewal or
hand-back code in this repository: the loop is imported and only the way a job is
executed is this runtime's own.

You do not normally type it. When you connect a machine, the Papaya client finds this
checkout, reads `ppy capabilities --json`, and execs `ppy serve` with the flags it
would have passed its own listener: `--supervised`, `--harness`, `--approval-timeout`
and `--working-directory`. Any other `listen` flag is ignored with one warning line on
stderr, so a newer client cannot fail to launch an older runtime. Under `--supervised`
stdout carries the JSON Lines protocol and nothing else — every log line goes to
stderr — and the opening `hello` carries a `runtime` field naming this runtime and its
version, so a host never has to infer what answered. The connection registers with
Papaya as `papaya-agent-runtime` whatever `--harness` says, so the app can tell a
machine running the manager from one running a bare harness.

Run it yourself with `./bin/ppy serve` (add `--working-directory <path>` if the
connection has no directory stored). Only one `serve` or `supervisor serve` may own a
`PPY_HOME`; a second start refuses and changes nothing.

A start on a checkout that has never been set up sets it up first — the same
non-interactive path `ppy setup` runs, with the providers taken from the
connection's harness — and says so in one line on stderr. It then checks whether it
can actually work and, if anything is blocked or missing, sends the owner one
direct message saying what needs them; the message is keyed on *which* problems
there are, so restarting doesn't repeat it. A blocked runtime still starts and
still listens: it declines the tickets it cannot place so a peer can take them,
which is a far better failure than a manager that would not come up until somebody
signed a harness in.

The lease identity survives restarts. Papaya's reservation is acquire-or-extend and is
keyed on a session id, so `ppy serve` stores one id per Papaya connection in
`.ppy/papaya-sessions.json` and passes it back on every start: a restart *extends* the
leases this machine already holds instead of racing them. Delete that file and the
next start mints a fresh identity, which means waiting out the leases of the last one.

What a picked-up ticket does today is deliberately small. The manager records the work
item as a task, writes the phase `picked_up`, says so through the client's
`Job.report_progress` (a `job.progress` message to a supervised host, a log line to a
terminal one), and then **holds the lease** until the
client stops the run — a hand-back from the app, a lease a person released, the stall
grace expiring, or this process shutting down — at which point it records why
(`handed_back`, `stalled`, `released`). A ticket it cannot place, because no repository
resolves or the runtime is not ready to work, is declined so a peer may take it. The
turns that brief, dispatch, review and deliver come next.

## Where state lives

All working state is under `.ppy/` (gitignored):

- `config.toml` — driver profile, worker defaults and ceiling, active-worker limit,
  cost posture, authority, self-assessment cadence, and the tool profile Claude
  workers launch with (`claude.allowed_tools`; `PPY_CLAUDE_ALLOWED_TOOLS` overrides
  it for a session).
- `state.db` — SQLite source of truth (repos, runs, tasks, decisions, sessions,
  events, usage, reviews, self-assessment cycles).
- `repos/` — read-only base clones. The only repositories work happens in.
- `runs/` — per-run plans, task packets, results, append-only event logs.
- `memory/` — durable notes in two tiers, per-instance and per-repo.
- `papaya-sessions.json` — one listener session id per Papaya connection, so a
  restarted `ppy serve` extends its own leases rather than racing them.
- `tools/`, `worktree-pools/`, `run/` — companions, task worktrees, supervisor sockets.
- `probes/` — raw provider-probe evidence.

The Papaya connection itself is owned by the client, not by this runtime:
`~/.papaya-agent/` for a terminal connection, or the desktop app's own
application-support directory. `ppy papaya status` says which one it found, and
`--json` lists everywhere it looked.

## Model routing

You pick a driver profile, a deterministic worker default, and a hard worker ceiling
— provider, top model, top reasoning. Omitted task choices use the worker default;
they never inherit the signed-in account's CLI defaults. Explicit lower-cost profiles
are allowed below the ceiling; the runtime never automatically selects a more
expensive one.

```sh
ppy config models \
  --manager-provider codex --manager-model gpt-6-astra --manager-reasoning xhigh \
  --worker-provider codex \
  --worker-default-model gpt-5.6-sol --worker-default-reasoning high \
  --worker-max-model gpt-5.6-sol --worker-max-reasoning xhigh \
  --worker-max-concurrent 2
```

Changing the driver profile affects the next launch; it cannot change the model of a
running chat. The recognized Codex safety order is Luna < Terra < Sol < Astra; an
unknown custom name is accepted only when it exactly matches the configured ceiling.

`worker.max_concurrent` is an admission cap for normal operation under one supervisor
per `.ppy` home. It counts active provider executions, not completed worktrees
awaiting review. This iteration provides no cross-process lock, distributed
scheduler, or durable automatic-continuation retries; do not run multiple supervisors
for one home. The cap is an operational guardrail, not a dollar budget.

Codex workers receive their concrete model and `model_reasoning_effort` on start and
resume. The current Claude adapter does not forward configured reasoning effort, so
the effort-controlled team above uses Codex; the runtime does not invent Claude effort
mappings it has not verified.

## What it checks with you first

It handles normal, reversible work on its own authority: inspecting, decomposing,
worktrees, delegating, testing, bounded retries, commits, and opening an
already-reviewed pull request. It comes to you for:

- genuine product or direction calls, and conflicting requirements;
- scope beyond what you asked for;
- new credentials or access;
- destructive, irreversible, or production actions;
- changes to model / reasoning / spend ceilings; and
- **merging** — off unless you grant a standing policy.

These are enforced in `ppy`, not in a prompt.

## Working on the runtime itself

Building Papaya Agent Runtime (rather than using it) follows the development contract
in [`AGENTS.md`](AGENTS.md). A harness opened here defaults to runtime mode, so if
you're hacking on the framework, edit `src/`/tests/docs as usual or set `PPY_DEV=1`
to switch explicitly.

```bash
make test        # hermetic tests (no provider spawning)
make lint        # ruff check
make fmt         # ruff format
make test-live   # opt-in: spawn real Claude/Codex probes
make probe       # re-run the provider capability matrix
```
