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

`ppy papaya connect` runs the connect flow — your only step is clicking Approve in the
browser.

### Without Papaya

Open Claude or Codex in this directory on a machine with no Papaya connection and the
runtime is still a manager, the way Middle Manager is: register and onboard repos,
brief, dispatch, steer, answer, review, deliver, gate runs, budgets, hygiene, PR
following, self-reported issues, config ownership, learned tools, `board`, `health`,
`handoff` all work exactly as they do connected. It says so once — at session start, on
`ppy start`, `ppy status` and `ppy doctor`, and when `ppy serve` starts — in one line:

    Running without Papaya. It's better with it: tickets, comments and the team's record flow in and out by themselves. https://trypapaya.ai

What is off without a connection:

- work items: no pickup, no status changes, no comments, no acceptance criteria on the
  item. A local task records each skipped ticket step as a `ticket_step_skipped` event;
- `ppy serve`'s sweep and event loop (it runs the rounds, the supervisor and the blockers
  ledger, and its start line says what is off);
- DMs to the connection's owner, and the workspace's identity, rules and memories.

Readiness lists `papaya_not_connected` as `info`: never blocking, not a blocker, and a
ready runtime stays ready. Connect later and the next `ppy serve` start picks it up.
`PPY_QUIET_INVITE=1`, or `papaya.invite = false` in `config.toml`, turns the line off.

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
  guessed at. It also records the repository's **gate policy**: whether its pre-push
  hook runs the suite, a scoped local gate from its own quicker targets (`make
  test-unit`, a `test:unit` script, else its test command; `--local-gate` to choose
  one yourself), its full suite, and who owns that (CI, the hook, or the supervisor).
  A repository with neither a local gate nor a hook is a readiness warning.

## Gates longer than a tool call

A harness caps a tool call at ten minutes and moves anything longer to the background,
where it dies with the session. So **a command that may run longer than ten minutes
is never run as a tool call.** `ppy gate run [<repo>] [--task <id>] [--full]` asks the
supervisor to run the gate as its own process, with no timeout; the call prints a
progress line every minute and answers within nine, exiting 0 (green), 1 (red) or 75
(still running; the same command again attaches to it). Every result is recorded on
the task against the head commit it ran at, with the command, duration, summary line
and exit code, and `ppy serve` decides on that record: a worker with a green gate at
its head is reviewed, a red one is steered with the summary, and one that stopped with
none is steered to run `ppy gate run`.

Heavy gates run one at a time per repository. A full suite (`--full`), or any gate whose
learned duration in its repository (p90, or a budget override) is past
`gate.parallel_ceiling_seconds` (300), takes one of the repository's
`gate.full_slots_per_repo` slots (1) before it starts. A second one queues behind it in
arrival order: its call exits 75 while queued and says so ("queued behind task 12's full
gate, started 4 min ago"), and running it again attaches as usual. Scoped gates never
queue, and full gates in different repositories run side by side. The supervisor samples
each gate's process group for peak resident memory, keeps it with the result and as a
`gate_memory` observation, and the head of a repository's queue also waits while the
machine's free memory is below that repository's p90 ("queued for memory: 812M free,
..."); `gate.min_free_mb` sets that floor instead. `ppy status --team` and the
liveness line show a queued gate as queued, the rounds do not treat it as silence, and
neither a gate's duration nor a worker's silence observation counts the time queued.

A gate runs in its own database, never the supervisor's or another gate's. A task's gate
gets exactly the environment its worker gets, rendered for that task id: the compose
project `task_<id>`, its database port, the database URLs from the repository's
templates, and private tool caches, with the supervisor's own `VIRTUAL_ENV`,
`DATABASE_URL`, `TEST_DATABASE_URL`, `COMPOSE_PROJECT_NAME` and port variable dropped
first (a stack declared after the task was dispatched is settled and recorded then). The
result records the compose project and database it ran against, and the call says them
before it starts. `ppy gate run --task <id> --baseline <sha>` (or `<repo> --baseline
<sha>`) gates a base commit instead: in a scratch worktree of that commit, with a
compose project and database of its own (`gate_base_<local|full>_<sha>`), both removed
when it ends, and recorded with `baseline: true` on no task, so it is never a task's
verdict. A gate with no task, in the base clone, is private the same way. Readiness
warns `gate_env_not_isolated` for a repository whose gates could still share a
database: one that ships a compose file but declares no stack, or a compose stack with
no port base or with database URL templates that lack `{task_id}`.

A gate is not re-run forever. Each result keeps the tests its output names as failed
(pytest's `FAILED`/`ERROR` lines). When the two newest results at one head are red with
the same failing tests in the same environment, `ppy serve` stops sending the worker back:
the ticket task records `needs_a_person` (a `gate_needs_a_person` event with both
results), the work item gets one comment naming the failing tests and the head, and the
review turn decides with that fact, delivering with the failures named as pre-existing
when a `--baseline` run shows them on the base, or handing the ticket back. `ppy gate run
--task <id>` refuses a third run at that head; a baseline is still allowed.

## Waits come from each repository's history

One timeout fits no repository. A backend suite outlasts the tool cap, a frontend
worker's first ten minutes are an install, and a client gate takes forty seconds. So the
runtime keeps every duration it already sees, per repository, where it ends: gate and
full-suite runs (including a push through a pre-push hook that runs the suite), worker
sessions, a worker's plan phase, the silence between its progress notes, brief and
review turns, and CI on delivered pull requests. From the newest 20 within 14 days it
derives a **budget**: the 90th percentile × 1.5, never below the configured default,
never above a ceiling (gate 2h, worker session 6h, plan 45m, silence 90m). With fewer
than three observations the default applies. A stalled or killed run is kept but never
derived from.

The waits read those budgets. The rounds' quiet and plan check-ins use the worker's
repository's silence and plan budgets, and the midpoint check-in uses half its
worker-session budget. `ppy gate run` says how long the gate usually takes and says once
when a run is taking longer than usual. A turn that ends `WAITING:` on a repository
whose gate is known to be long waits that long before it reruns. The worker's
environment block says how long the gate has taken there, and readiness warns when a
repository's gate budget is longer than the ten-minute tool cap.

`ppy repo budgets [<repo>]` prints each kind's observation count, p90 and budget, and
whether it is `derived`, `default` or `override`, and the p90 of the repository's gate
peak memory (`gate_memory`), which is a measure a heavy gate waits for, not a time budget. `ppy repo set <repo> --budget
<kind>=<seconds>` sets an override, which wins over the history; `0` clears it.

## Work reaches the remote as it goes

A worktree is one machine's disk; a restart strands whatever is only there. Every
brief, every worker's environment block and the Claude command rules carry one rule,
word for word (`prompts.PUSH_MILESTONE_RULE`): commit and push after each goal in the
brief lands with its scoped gate green, and in any case before a run that may exceed ten
minutes. `ppy serve`'s rounds check in on a worker whose HEAD is not on the forge and
whose branch has had nothing new there for `health.push_by_minutes` (below). The review
reads the remote branch, never
the worktree: a worker that says done with uncommitted files is sent back with
"uncommitted work in the worktree: <n> files" to commit and push or discard them, and
is reviewed once its branch holds the work.

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
ppy blockers                            # what this machine needs from a person, as commands
ppy papaya status                       # which Papaya agent this machine is
ppy papaya connect                      # sign in and pin this machine to an agent
ppy repo discover                       # repos on the forge that aren't registered yet
ppy repo add https://github.com/you/your-repo
ppy repo onboard your-repo              # learn what it is, its build, tests, gate policy
ppy gate run --task <task_id>           # a gate under the supervisor, past any tool timeout
ppy repo budgets your-repo              # how long things take there, and how long it waits
ppy repo locate "hover card"            # which registered repos contain these strings
ppy serve                               # the always-on manager: supervisor + Papaya loop
ppy sweep                               # ask the running serve to look for assigned work now
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

**What the machine needs from its owner reaches its owner.** Some things only a person
at the machine can fix: `gh` not signed in to a forge a registered repository (or this
checkout's own origin) lives on, `gh` not installed, no signed-in `claude`/`codex`,
Node or uv missing where a repository needs them, Docker stopped for a compose
repository, the disk under 5 GiB free, a repository the signed-in GitHub account cannot
read or push. (No Papaya connection is not one of them: see *Without Papaya*.) Readiness
finds each of these as a *blocker* with a
title and the literal commands that close it, in order. `ppy serve` keeps them in
`.ppy/blockers.json`, re-checks every round (nothing to restart once a person has done
their part), and tells the owner when one appears, again only when its steps change or a
day has passed, and once when it clears — in the agent's DM with the person who
connected the machine, as `runtime.blockers: [{code, title, steps, since}]` on the
supervised `hello` and every `status`, and at start on stderr. A ticket refused because
of one (a pickup on a signed-out forge, a delivery that could not open its pull request)
is handed back with one neutral comment — "This machine needs setup before it can take
this; its owner has been told what to do" — that names nothing. Every string on those
surfaces is redacted of tokens, home paths, email addresses and diff hunks, and the
machine is named only by its short hostname. With `forge.github_oauth_client_id` set in
`config.toml` (Papaya's GitHub OAuth app; not a secret), the runtime signs `gh` in
itself through GitHub's device flow: the owner is sent the code to enter, and the token
goes into `gh auth login --with-token` on stdin and nowhere else. `ppy blockers` (exit 1
when there are any) and `ppy doctor` print them locally.

**The environment is built once, never under a running `serve`.** `bin/ppy` runs every
command with `uv run --no-sync`, so typing `ppy status` in a terminal cannot rebuild
`.venv` out from under the manager the desktop app started. Only three things sync:
`ppy serve` as it starts (only when the environment was built from another lockfile or
does not import), an explicit `ppy env sync`, and the first command on a checkout with
no environment. Each takes the supervisor's lock (`.ppy/run/supervisor.lock`) for the
length of the sync and refuses, naming the pid, while a `serve` or `supervisor serve`
holds it; a lock file naming a pid that is no longer running is taken, with one line
saying so. A sync never touches the environment in use: it builds a fresh
`.venv.env-<stamp>` beside it, checks the Papaya client imports from it, and swaps the
`.venv` symlink over in one rename, so a refused, failed or interrupted sync leaves the
previous environment importable. `ppy readiness` reports `environment_broken` when the
client is missing from it, and the next `ppy serve` start rebuilds it before anything
else. The launcher also passes `--python` from `.python-version`, so the app's uv and a
shell's uv ask for the same interpreter series; `ppy doctor` warns when the
environment's `pyvenv.cfg` disagrees with it.

`ppy supervisor stop`, `ppy supervisor status`, `ppy version`, `ppy doctor` and
`ppy blockers` never sync: they run from the environment when it imports and from the
source tree (under uv's interpreter for the pinned series) when it does not, so the
command that clears a supervisor in the way always works.

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
`PPY_HOME`, and a new `serve` takes over from whatever holds it without a person. The
owner records its build in `.ppy/run/supervisor.json` (git head, package version, start
time). A new start that finds a live supervisor **of this checkout's build adopts it**:
it connects and carries on, and its workers keep running. One **of another build** (the
checkout was pulled), or one that recorded no build, **is retired**: it is asked to shut
down, given `supervisor.stop_timeout` for its workers, then sent SIGTERM and SIGKILL if
it will not go; a fresh supervisor starts, and the rounds' reclaim resumes each stopped
worker from its session in its worktree. The launcher does this *before* it syncs, so a
sync is only ever refused for a supervisor the start chose to keep. One stderr line says
what happened and which tasks resume. If a start still cannot go on, it prints one
sentence naming the cause and what it needs, exits 1 (never 75), and records it for the
blockers ledger; the next start that succeeds reports it to the owner once.

**Stopping `serve` stops what it started.** On SIGTERM, SIGINT, SIGHUP, the client's
`shutdown` or `ppy supervisor stop`, `serve` stops listening, asks its supervisor to shut
down, and waits up to `supervisor.stop_timeout` (default 30s) for every worker to be
recorded `worker_stopped` with its session kept for a resume, then exits. Workers, gates
and manager turns keep process groups of their own (so an interrupt reaches the tools
they started and never `serve`), and a small watcher process — the lifeline — kills
whatever is still running if `serve` itself dies without running another line (an app
crash, `kill -9`), so no orphan is left holding a worktree.

**A runner row is truth or it is closed.** Worker slots are counted from the runner rows
that say `starting` or `running`. A row whose process is gone — its pid is not alive, or
it never had one and has not been heard from for `supervisor.dead_after` (default 600s)
— is closed at every supervisor start (fresh, adopted or retired), at every `serve`
start, and on every manager round whatever its ticket's state: the row becomes
`exited`, its slot comes back, and a task still in flight is recorded `worker_stopped`
with the cause and its session kept for a resume. One line per row.

**A base clone is its forge's.** `ppy repo add` clones from the forge even when given a
local path (the path only says where the forge is, through its own `origin`, and seeds
memory), so the clone's `origin` is the forge. The default branch is the forge's HEAD
(`git ls-remote --symref`), never the branch a checkout happens to be on, and the clone
is checked out on it; a path registration whose checkout is on another branch says so.
`ppy repo sync` rewrites an `origin` that points at a local path to the forge and
corrects a stored default branch that differs from the forge's HEAD, saying each change;
`ppy repo set <repo> --default-branch <branch>` pins one over the forge (an empty value
unpins it). Each `serve` start makes both repairs, one line per repository repaired. A
clone whose `origin` is still a local path because the forge could not be reached is
refused work and reported as the `repo_origin_is_local` blocker, with its steps.

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

### A ticket, from pickup to pull request

A picked-up ticket is **worked**, not just held. The process that holds its lease
sequences it through these phases, and writes each one on the task row, as a
`ticket_phase` event (so the order survives), and as a `Job.report_progress` line (a
`job.progress` message to a supervised host, a log line to a terminal one):

```
picked_up -> briefing -> dispatched -> reviewing -> delivering -> reported -> released
                 |         |    ^  ^         |
                 |         v    |  +- steer -+
                 |        blocked
                 |   (answer turn, or a person's reply)
                 |
                 +-> declined   (any turn that misses its job twice hands the ticket back)
```

| Phase | What moves it on | Who acts |
|---|---|---|
| `picked_up` | the job is approved and the ticket recorded | runner |
| `briefing` | a worker task appears in the ticket's run | **brief turn** |
| `dispatched` | a worker's `worker_done`, question, stop or failure | the worker |
| `blocked` | an answer or steer on the worker, or a person's reply | **answer turn** |
| `reviewing` | a delivery (on to `delivering`) or a steer (back to `dispatched`) | **review turn** |
| `delivering`, `reported` | the pull request is open and the result posted | runner |
| `released` | the hold ends: done, lease lost, or shut down | client |
| `handed_back`, `stalled`, `declined` | the app took it back, nothing happened for too long, or a turn missed twice | client / runner |

**Judgment lives in the turns, never in the runner.** A turn is a headless session of
the configured manager harness, built by the same launcher as `ppy start`, run in this
directory with the client's job environment (persona and memories, the plugin, the
activity stamp) and a write boundary of this directory alone, so it can register and
dispatch but never edit a repository by hand. Its prompt is reviewed text under
`src/papaya_agent_runtime/prompts/` and points at the skills it follows:

- **brief** reads the work item, **chooses the repository** — the item names it; the
  agent already knows (memories, and each repository's "What it is" notes, which
  `ppy repo onboard` now fills); the code says (`ppy repo locate "hover card"`);
  `ppy repo discover` and register; ask on the item and wait; then record the mapping —
  writes acceptance criteria onto the record if it has none, and dispatches a brief
  written to `brief-a-worker` with `ppy dispatch --brief --strict`;
- **answer** unblocks a worker's question with `ppy answer` or `ppy steer`, or takes it
  to the person who can answer;
- **review** follows `review-a-worker`, runs the gate at head, then approves and
  delivers or steers with every finding, and posts the result on the work item.

The runner knows a turn did its job from the ledger alone — a worker in the ticket's
run, or an answer, steer or delivery event since the turn began — and retries a turn
that did not once, with the tail of its transcript, before handing the ticket back.
**Waiting is not a miss.** A turn runs its gates in the foreground; one whose gate
cannot finish inside the turn ends with a message whose first line is
`WAITING: <what it is waiting for>`. The runner reports that as progress, keeps the
ticket in `briefing` or `reviewing`, and runs the turn again with its tail after five
minutes, doubling each time to a cap of thirty; only a turn that ends with no dispatch,
delivery, steer, question or `WAITING:` counts toward the hand-back. A long gate is the
worker's job, not the review turn's: a worker whose session ended mid-gate is steered by
the runner to run its gate to completion and report (twice at most, then the review turn
gets the failure), so the review turn normally runs on `worker_done` and re-checks a gate
result that is already on the record.
A worker pool that is full is not a miss: the ticket waits in `dispatched` and is never
handed back for it. A restarted `serve` given a ticket it was already working picks it
up from its last working phase rather than briefing it again.
While a ticket is `dispatched`, `reviewing` or `blocked`, a new comment on it from
anyone but this agent is read within a minute and starts the **answer** turn with the
comment in its facts (after any turn already running), and is recorded so that not
even a restart answers it twice.

The work item's **status** is state, so the runner sets it: `in_progress` on pickup,
`review` when the pull request is open, `blocked` while a question waits on a person,
and `todo` on hand-back, with `handed back: <reason>; branch <name> kept`. Everything
that needs judgment is said by a turn.

**What the ticket shows, and where the logs are.** A turn runs as the Papaya agent:
it gets the MCP config `papaya-agent mcp runner-config` writes for this connection
(with `--strict-mcp-config`, so no other `papaya` server loads) and the client's
plugin, the same as the client's own runners give a job. Its stdout and stderr are
kept at `.ppy/runs/<run id>/turns/<turn>-<n>.log` (for example `brief-1.log`,
`review-2.log`), and the path appears in the ticket's progress as soon as the turn
starts. On the work item itself the runner posts only what a person reading the
thread needs, one line each, as the agent: picked up, dispatched (worker and
repository), blocked (the question) and unblocked, and — the first time a review
sends the worker back, never again — `Sent the worker back with findings; still
working.` The report is the review turn's own comment, and the runner posts nothing
after it. The web app's ticket card shows the latest line as the agent's status.
Worker progress and the review loop's bookkeeping (reviewing, stopped short, sent
back to its gate, pull request open) are progress lines to the host, in the app and
the log. The runner also checks the turns' promises on the record: a brief with
Goals must leave acceptance criteria on the item, and a delivery must leave the
review turn's own report as a comment. A miss runs the turn once more with a
one-line reminder. If the report is still missing, the runner posts
`Pull request open: <link>; see the pull request for details.` itself, as the last
line.

A ticket this machine cannot take at all — a repository the item names that cannot be
registered, or a runtime that is not ready to work — is declined before any of that, so
a peer may take it. An item that names no repository is *not* declined: choosing one
is the brief turn's job, and nothing ever falls back to this checkout.

The manager also **looks for work** instead of only waiting for it. An event can be
missed — the machine was off, every slot was busy, an older client aged it out — so on
start, and then every five minutes, `serve` asks Papaya which work items are assigned
to this agent and still open (`todo`, `in_progress`, `blocked`, `changes_requested`).
It offers each one that has no live task here to the client's loop
(`ListenerLoop.offer`). An offered ticket goes through the same reservation,
supervised approval and runner as an event does. A ticket whose earlier task was
handed back, declined, released or closed counts as not live, so it is offered again.
One held by another session is skipped quietly until the next sweep. An `in_progress`
ticket touched in the last six hours (`PPY_SWEEP_STALE_AFTER` seconds) is being worked
elsewhere and is skipped as `in progress elsewhere`. Items are offered most important
first: priority (urgent, high, normal, low), then `changes_requested` and `blocked`,
then `todo`, then `in_progress`, then the oldest `updated_at`. A full pool ends
the round early. A ticket this runtime *declined* leaves no task, so the decline is
remembered in `.ppy/sweep-declined.json` along with the ticket's `updated_at`. The
sweep leaves that ticket alone until someone changes it (an edit or a comment moves
`updated_at`) or a person runs `ppy sweep --include-declined`. Without this, the app
would ask about the same unplaceable ticket every five minutes. Papaya sends work to
one person's machines and keeps the rest with the agent in Papaya, so a reserve for an
item not sent here is refused. The sweep remembers that refusal in
`.ppy/sweep-kept.json` with the item's `updated_at`. It does not ask again until
`updated_at` moves, 30 minutes pass, or a person runs `ppy sweep --include-kept`
(the same flag as `--include-declined`). Picking an item up clears both memories.

A claim is not work, though. Kept work is left alone only while something shows
somebody doing it: a live reservation on the item, a live agent job, or a comment or
status change by the holder within `sweep.idle_claim_minutes` (15 by default, in
`config.toml`). With none of those the item is **idle**: the memory is skipped and the
sweep asks for it on every sweep. If Papaya still refuses (after its on-call fallback
takes an item, it refuses the owner's machines for a guard window), the sweep keeps one
blocker for the owner — "Papaya keeps 3 idle items from this Mac: PAP-219, PAP-221,
PAP-222; use Run on this Mac, or wait for the guard to lift" — changed when that set
changes and cleared when it empties. An item refused on three sweeps running with no
evidence of work is also recorded as an `idle-work-refused` deficiency.

On start, and on the first sweep after Papaya could not be reached, the sweep also
**takes back what an earlier connection of this runtime held**. That is any open item
with a ticket task here that was not handed back, declined or finished; a reservation
or a Run on this Mac hold naming a connection id in `.ppy/papaya-sessions.json`; or an
item with an on-call fallback note where this runtime's since-revoked connection had
commented. For each one it calls Papaya's reclaim route
(`POST /agent-client/workspaces/{id}/work-items/{item}/reclaim`; a server without it
answers 404 and the offer falls back on `reserve`), then offers the item. The ticket
resumes from where its task stood: a stalled ticket whose worker is done goes straight
to review, and a live worker is watched again. It never takes an item that a
reservation held by somebody else, or a live job, shows being worked. It writes one
line per item and one summary line (`reclaim on connect: 3 held by an earlier
connection, 1 reclaimed, 2 refused`).

Each sweep writes one line on stderr saying why it left each item alone, for example
`sweep found 23: 4 kept by Engineering Agent in Papaya and being worked, 18 kept by
Engineering Agent in Papaya and idle for 40 minutes (use Run on this Mac to route one
here), 1 declined earlier, 0 offered`. Other reasons are `in progress elsewhere`
and `already taken` (a live task here or another session's hold). A sweep that finds
exactly what the last one found writes at most once every 30 minutes. Set the cadence with `--sweep-interval SECONDS` or `PPY_SWEEP_INTERVAL`
(`0` sweeps once, at start), and run `ppy sweep` to have the running `serve` sweep
now and print that line.

### What the manager does every five minutes

Reacting to events is not enough. A worker goes quiet, a person never answers, a pull
request turns red after delivery. So `serve` also **does rounds**: every
`--rounds-interval SECONDS` (`PPY_ROUNDS_INTERVAL`, or `health.rounds_interval` in
the config; 300 by default) it walks the board on the same event loop. It holds the
sweep's lock, so a round and a sweep never overlap. In order, a round:

1. **Reads the forge** for delivered tickets, and follows each pull request until it
   merges (below). A merged pull request moves the ticket to `done` with one comment
   and cleans up its worktree at once.
2. **Takes back what it was working and no longer holds.** On start, and after a lost
   lease, every ticket in a working phase is re-reserved under the persisted session
   id and resumed from its recorded phase. A worker that is still running is watched,
   never re-dispatched. A ticket someone else now holds is closed here as
   `handed_over`, with its branch kept and nothing posted. Once per start, tickets
   handed back because a turn "ended without" doing its job (PAP-213) are offered
   again, with the earlier worker's branch in the brief's facts. A ticket whose hold
   the client ended as `stalled` counts as working too when its worker's work went on:
   a live session is watched again, a `worker_done` goes to review, and a
   `worker_stopped` with its branch ahead of base goes back to its gate. None of those
   runs a new brief or a new dispatch.
3. **Looks at every worker it holds**, using `ppy health`'s facts:
   - **Dead session with no done note.** The worker is recorded as `worker_stopped`,
     and the runner's gate steer (`ppy gate run`) takes it from there.
   - **A question.** A worker whose status is `blocked`, or whose last note asks
     something, gets the answer turn.
   - **Stopped short.** A stopped worker whose branch is ahead of base and has no gate
     recorded at its head goes to that same gate steer.
   - **Check-in.** A worker gets the **check-in turn** if it is silent past its
     repository's silence budget with a live session, still planning past the plan
     budget, has run for half its worker-session budget, or has run
     `health.push_by_minutes` (45) with nothing new on its remote lease branch. Without
     history the first three are `health.quiet_minutes`, `health.plan_minutes` and
     `health.checkin_after` (20). For the push one, every round asks the forge for the
     lease branch (`git ls-remote` against the repository's `forge_url`, or the
     worktree's `origin` when it has none), never a remote-tracking ref, and records when
     it first sees a new tip (`push_seen`): that is the last push. The push check-in
     fires when the worktree's HEAD is neither that tip nor behind it and no new tip has
     landed for `push_by_minutes` since the session started or the last push. A worker
     whose HEAD is on the forge is never nudged for it, whatever else it is doing, and
     uncommitted files do not count (the review deals with those). After a restart, a tip
     no round has seen yet counts as pushed when first seen, so a check-in can come late
     but not wrong. It says "nothing pushed in N minutes", comes back after every further
     `push_by_minutes` with no push, and the turn steers the worker to commit what is
     green and push before it continues. The turn
     reads the brief's Goals and the whole progress log, then ends with one line:
     `CHECK-IN: continue`, `CHECK-IN: steer <message>` or
     `CHECK-IN: stop and resume with <message>`. The decision and why the check ran are
     recorded on the ticket (`ticket_checkin`). A push check-in's record and its turn's
     facts also carry what the round saw: the forge's tip (`remote_sha`), the worktree's
     HEAD (`head_sha`) and the last recorded push (`last_push_at`).
   - **Gate running.** A worker whose gate is running under the supervisor is not
     silent, and is left alone.
   - **Person wait.** A question that has waited 15 minutes on a person is said once on
     the ticket (`waiting on you: …`), and the ticket is `blocked`.
4. **Tidies worktrees**, at most once an hour. It uses `ppy worktree prune`'s rules:
   only finished tasks, a clean checkout, every commit on a remote, and a base clone
   under `.ppy/repos`. Then it runs `git worktree prune` and `git fetch --prune` on the
   base clones. Each run records a `worktree_hygiene` event with what it removed (in
   bytes) and what it kept (with the reason). A kept slot that is finished, dirty or
   unpushed, and a day old becomes one "waiting on you" item.

The round only decides *when* to look and *what facts* a turn gets. It never writes a
message for a worker: every continue, steer, stop and answer comes from a turn. A round
writes one progress line for each ticket whose state it changed and one `round:` line
on stderr. A round that finds nothing writes nothing.

### A busy worker is not a stalled job

The client calls a held job stalled after 30 quiet minutes and hands it back 10 minutes
later. It counts only progress lines and a harness's own output as activity, and a
manager's worker is neither. So while a held ticket's worker session is live, or its
gate runs under the supervisor, `serve` sends one progress line at most every
`health.liveness_minutes` (5). The line comes from what the supervisor already records:
the tool call running and how long it has run (Claude's `tool_progress` heartbeat), for
example ``Worker task 9 active: `make verify`, 12 min in, third run``; the worker's
latest words; or ``Gate running under the supervisor: full suite `make verify`, 8 min``.
The line goes only to the job's progress log, never to the ticket. If there have been
no worker events since the last line and no gate is running, nothing is sent, so a
worker that has really gone quiet still stalls. What quiet means for a repository is
still the rounds' silence budget. Under `--supervised`, a `job.stalled` for a ticket
whose worker is active gets that line at once, which clears the stall before its grace
runs out. If the hold is handed back as stalled anyway, the worker keeps running, the
ticket's phase is `stalled`, nothing is posted, and the next round takes it back up.
A terminal `serve` has no protocol to hear `job.stalled` on, so there the regular lines
are the only defence.

### It opens issues on itself

When the runtime itself gets in the way (not your code, and not a worker's mistake on a
ticket), `serve` records a **deficiency** and opens a GitHub issue about it in the
runtime's own repository. Each deficiency is one row in the `deficiencies` ledger,
fingerprinted by its kind and its detail with the numbers and ids taken out. These are
the signals, recorded where they already happen:

- **A turn reports it.** Any turn (brief, answer, review, check-in) ends with a line
  `RUNTIME: <what got in the way>`. Every turn prompt invites that line when a tool was
  refused, a fact could not be found, or a contract was wrong.
- **Readiness can't be fixed.** A blocking readiness finding the runtime owns is still
  there after first-run setup.
- **A live worker stalls.** The client stalls a held ticket while its worker's session is
  still live: the liveness lines above did not reach it in time.
- **A turn misses its job.** A ticket is handed back because a turn ended twice without
  doing its job, or a worker's gate was backgrounded past the tool cap with no
  `ppy gate run` on record.
- **A tool is refused.** Workers in one repository are denied the same plain command
  twice, and learning cannot fix it: the program is outside the safe family
  (`tool_learning` learns the ones inside it), or the profile already allows it and the
  harness refused it anyway. Only this kind of denial, a `profile_gap`, is a tool
  issue. A `command_shape` denial (operators, a pipe, redirection, `FOO=1 cmd`,
  `cd … && …`) is the worker breaking the command rules: after two, that worker is
  steered once with the rules themselves, and three workers doing it in one repository
  in a day is one `prompt-clarity` issue about the rules text, with their commands as
  evidence. A `policy_refusal` (a program workers are never given, such as `sudo`,
  `curl` or `docker`) is counted per repository and never an issue; the worker is
  steered once with the rule it broke. A denial is recorded once per tool call, and a
  retry of the same line within a minute is the same denial. At start, `serve`
  re-classifies older `worker-denial` rows, and comments on and closes any issue whose
  denials were never profile gaps. It closes a `turn-report` issue the same way, with
  one comment, when every occurrence came from a check-in a later fix made impossible:
  the round record that started the check-in names only fixed triggers
  (`deficiencies.FIXED_CHECKINS`) and lacks the field each fix added (for `push`,
  `head_sha`).
- **Something crashes.** An exception escapes `serve`, a ticket's hold, a round, the
  sweep or the supervisor's worker thread. It is recorded with the traceback.
- **A steer doesn't stick.** A check-in steers one ticket twice for the same reason.
- **The runtime's own CI goes red.** CI fails on a pull request the runtime delivered to
  its own repository.

**One issue per fingerprint.** It is labelled `self-reported` plus the kind, and the
body has four parts: what happened, the evidence, what the runtime did instead, and a
proposed remedy. A deficiency that happens again adds one comment and bumps the count.
If its issue was closed, that comment reopens it instead of opening a duplicate.

**Where issues go, and how many.** The repository is the origin of the running
checkout, or `self_report.repo` in the config. At most `self_report.max_per_day` new
issues open a day (5 by default); the rest wait in the ledger for a later day.
`self_report.enabled = false` keeps the ledger and opens nothing. An origin that is not
GitHub keeps the ledger and logs one warning.

**Nothing private leaves the machine.** An issue names ticket keys and repository names
only. It never includes source, diffs, ticket titles or descriptions, comment text, or
people's names. Paths under your home directory become `~`, and tokens and email
addresses are removed.

**Seeing it locally.**

- `ppy deficiency list` shows the ledger with issue links; `--all` adds deficiencies
  still below their threshold and re-classified ones. It ends with the worker denials
  per repository that are never reported (`command_shape`, `policy_refusal`).
- `ppy doctor` shows how many self-reported issues are open.
- `serve` prints one line at start when some are waiting to open.

### A pull request is followed until it merges

Delivery is not the end of a worker's pull request. Every brief, every worker's
environment block and the Claude command rules say so, word for word
(`prompts.PR_FOLLOW_RULE`): the pull request is the worker's until it merges, it will be
steered back for red CI, conflicts, a behind branch or reviewer comments, it is fixed on
the same branch, never with a second pull request, and never force-pushed over a
reviewer's view without saying so.

Each round reads every open delivered pull request's **reasons**:

| Reason | When | What the worker is told |
| --- | --- | --- |
| Conflicts | `mergeable` is `CONFLICTING` or the merge state is `DIRTY` | rebase onto the base and resolve conflicts |
| Behind | merge state `BEHIND`, and delivery recorded that the base requires up-to-date branches (its ruleset or protection), or a merge was refused for it | update the branch |
| Red CI | a check failed | the failing checks, and their log tail |
| Stuck CI | a check pending longer than the repository's learned CI budget | CI stuck: rerun or look |
| Reviewers | changes requested; unresolved review threads and comments from a person since the last push (authors ending in `[bot]` are skipped) | the thread's file, line and first line |

The reasons and the pull request's head make a **fingerprint**. The same reasons at the
same head are raised once; a new push, a new failing check, a new conflict or a new
comment is new. A raise sends the ticket back to `dispatched`, the review turn gets the
facts and composes the steer, the worker fixes and pushes to the same branch, and
delivery records the new head.

PR fixes have their own capacity, the **reconcile lane** (`worker.reconcile_slots`,
default 1). The supervisor admits a run of any task that was ever delivered only in the
lane, never in a ticket slot (`worker.max_concurrent`, default 3), and never admits a first dispatch
there. So a fix starts with every ticket slot busy, and new tickets never wait behind
fixes. The lane takes one pull request at a time. The queue is ordered by how close
each is to merging (behind only, then conflicts, then CI, then reviewers) and then by
age. If the session that delivered the pull request can be resumed and its worktree
still exists, that session is resumed with the steer. Otherwise a fresh **reconciler**
session starts on the same task and branch from `prompts/reconcile.md`, a brief scoped
to that one pull request: its link, the base, the log tail, the conflicting files, the
open threads, the gate policy and the rules. If the lane fails twice at one head (the
attempt ended and the head did not move), the ticket is marked `needs_a_person` with one
comment and the reasons. It is not retried until its head or its reasons change.
`ppy status` prints the lane: `idle`, or which pull request it is fixing and for how
long, and how many are queued.

A pull request that is green, mergeable and has no changes requested is a state too.
After `delivery.merge_after_hours` (24) at one head, the ticket gets one comment: "PR
<n> has been green and unmerged for a day". On a repository with
`ppy repo set <repo> --auto-merge` (off by default; `--merge-method squash|merge|rebase`),
the runtime merges it instead, and the ticket goes to `done` the way any merge does.

### Copiloting the team from a session

A person can open a session in this checkout while `ppy serve` runs and work beside it:

- `ppy status --team` is the whole team in one picture, one line per item: held tickets
  with phase and age, each worker with its last tool, how long it has run and its last
  progress note, delivered pull requests with CI, review and the reconcile lane, blockers,
  the last round's summary, and what waits on a person. `--json` gives the same facts to
  a hosted tool or a script. The rounds record a summary line (`round_summary`) and each
  pull request's state when it changes (`pr_observed`), so both are on the record.
- `ppy tail [--since 10m] [--follow]` prints the daemon's events one line each: pickups,
  phase changes, worker notes, check-in decisions, hygiene, pull request attention,
  blocker changes, deficiencies and round summaries.
- `ppy steer`, `ppy answer`, `ppy resume` and `ppy stop` go through the same supervisor
  the daemon runs, and record `by: person` (a turn `serve` launched records
  `by: manager`). A round leaves a worker a person just steered alone until the worker
  files a note or one silence budget passes, and any later check-in turn gets the
  person's direction as a fact that stands.

From Papaya, a comment on a held ticket is the way to reach the work: the answer turn
reads it with the ticket's status line from the same record. @mentions and agent DMs
are the hosted agent's, never this machine's. Each held ticket is meant to carry that
status line as one comment edited in place; the runner has the hook
(`TicketRunner(status_comment=)`), but nothing is posted until Papaya lets an agent edit
its own comment (backend #636). Until then the phase comments are the ticket's record.
Without Papaya, or for a local task with no work item, the status line is never written;
`ppy status --team` and `ppy tail` work the same either way.

## Where state lives

All working state is under `.ppy/` (gitignored):

- `config.toml` — only what differs from the code's defaults: driver profile, worker
  defaults and ceiling, active-worker limit, cost posture, authority, self-assessment
  cadence, and the *changes* to the Claude worker tool profile. Setup writes nothing
  but `config_version` and the values a person chose, so a new release's defaults
  apply on the next start. The tool profile itself is code
  (`config.CLAUDE_PROFILE`): Python (`uv`, `python`, `python3`, `pytest`, `ruff`),
  JavaScript (`node`, `npm`, `npx`, `pnpm`, `corepack`), git, the launcher
  (`Bash(*/bin/ppy:*)`, any checkout path), and the everyday file and text verbs
  (`cp`, `mv`, `tee`, `touch`, `head`, `tail`, `wc`, `sed`, `find`, `sqlite3`, …).
  Workers launch with that profile plus `claude.extra_tools` minus
  `claude.dropped_tools`; `ppy config claude --allow/--deny <pattern>` edits them,
  `--show` lists every tool marked profile, extra or dropped, `--reset` clears them,
  and `PPY_CLAUDE_ALLOWED_TOOLS` overrides everything for a session. Containment, not
  this list, is the write boundary: a worker allowed `cp` is still refused outside
  its worktree.

  **The runtime keeps this file right by itself.** An older file (a verbatim
  `claude.allowed_tools` copy, stored defaults) migrates on load, once. At
  `ppy serve` start and whenever a repository is ensured, a gate tool that was dropped
  is restored, and a worker's denied command in the safe family
  (`tool_learning.SAFE_FAMILY`: read-only text tools, the language toolchains, file
  verbs inside the worktree, `ppy` by any path) is added to `extra_tools` for the next
  dispatch. A plain command outside the family changes nothing and becomes one
  readiness warning naming the pattern to `--allow`; a command refused for its shape
  or by policy never does, because no pattern would help. Every change is a `config_change` event:
  `ppy config history` lists them, and `ppy serve` start and `ppy doctor` print one
  line each. `ppy config claude --lock extra_tools` (or `dropped_tools`) stops the
  runtime changing a key; a change a lock refuses is a readiness warning naming it.
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
  --worker-max-concurrent 3
```

Changing the driver profile affects the next launch; it cannot change the model of a
running chat. The recognized Codex safety order is Luna < Terra < Sol < Astra; an
unknown custom name is accepted only when it exactly matches the configured ceiling.

`worker.max_concurrent` (default 3) is an admission cap for normal operation under one
supervisor per `.ppy` home. It counts active provider executions, not completed
worktrees awaiting review. `ppy serve` declares the same number to Papaya as the
tickets it holds at once, and the reconcile lane (`worker.reconcile_slots`, default 1)
is on top of it. A version-1 `config.toml` that stored the old default of 2 drops it on
migration and gets 3; a 2 a person set after that is kept. This iteration provides no cross-process lock, distributed
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
