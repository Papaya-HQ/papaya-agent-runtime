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

A work item is for something a person would look for later: a feature, a bug worth a
record, a proposal, something awaiting sign-off. The tasks dispatched to get there
live in the runtime's own ledger. Minting a work item per task is noise, and the
runtime is built not to. What it does do is keep the items that exist current, and
link the tasks that belong to one (`ppy papaya link`) so the pull request body names
the work it was for.

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
- **Python 3.12+**, **Git**, and **[uv](https://docs.astral.sh/uv/)**.
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

### The control plane, if you want to look

You don't type these — the runtime does — but nothing is hidden:

```bash
ppy doctor                              # environment, capability drift, Papaya connection
ppy papaya status                       # which Papaya agent this machine is
ppy papaya connect                      # sign in and pin this machine to an agent
ppy repo discover                       # repos on the forge that aren't registered yet
ppy repo add https://github.com/you/your-repo
ppy repo onboard your-repo              # learn its build, tests, CI gate, conventions
ppy supervisor serve                    # per-task runners, durable state
ppy dispatch --repo your-repo --brief brief.md --provider claude
ppy worktree list                       # every leased slot: task, state, size
ppy review show <task_id> && ppy review approve <task_id>
ppy deliver <task_id>                   # refused unless approved at current head
ppy papaya link <task_id> --work-item PAP-148 --url ... --title "..."
ppy answer <task_id> --answer "use /v2/health" --scope run   # recorded + reused
ppy plan <run_id>                       # cross-repo rollout order
ppy usage <run_id>                      # model token accounting
ppy assessment status                   # performance-review cadence
```

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
