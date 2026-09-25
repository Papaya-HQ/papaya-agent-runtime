# Papaya Agent Runtime

![Papaya Agent Runtime](docs/PapayaAgentRuntime.png)

**The home you point a Papaya connected agent at when you want it to build code.**

Connect a machine to your Papaya workspace as your engineer and point it at this
checkout. From then on it works the tickets assigned to that agent end to end:
isolated worktrees, parallel workers across every repository you own, an
exact-HEAD review gate, and pull requests that arrive already reviewed. It has no
persona of its own. It is whichever Papaya agent the machine is connected as, and it
reports where the ticket is discussed.

## Getting started

Five steps. The first is in Papaya, the setup itself is done by the CLI you already
use, and the runtime runs as a connected machine.

### 1. Create your engineer in Papaya

The runtime runs *as* an agent in your workspace, and the one to use is your own
engineering agent. In Papaya, open **Agents → New agent** and create one from the
engineering template. It comes out as "<your first name>'s Engineer", with the
persona, rules and memories the runtime will carry. If you skip this step, the
connection in step 3 creates it for you: your CLI connects with
`ppy papaya connect --create-engineer`, and the `ppy setup` picker offers **Create
your engineering agent** as its first row.

### 2. Clone this repository

```bash
git clone https://github.com/Papaya-HQ/papaya-agent-runtime
cd papaya-agent-runtime
```

The machine needs git, [uv](https://docs.astral.sh/uv/), the `gh` CLI, and Claude
Code or the Codex CLI, on macOS or Linux (Windows only inside WSL2). Everything else
is installed for you.

### 3. Let your CLI set the machine up

Open the harness you use in the checkout, once, and say what you want built:

```bash
claude      # or: codex
```

Before it answers, it runs its own first-run setup. It builds the Python
environment, installs the pinned companion tools, connects this machine to Papaya as
your engineer, creating it if you have none (a browser approval, or a device code over
SSH), asks the two or three
questions that shape the team (who drives, the worker default and ceiling, how
cost-conscious to be), and names the repositories it found on your forge so you can
say which ones it may work on. Anything only you can do, such as signing `gh` or the
harness in, comes back as one command to run.

Prefer a checklist to a conversation? `./bin/ppy setup` runs the same steps and
says one line for each; `./bin/ppy setup --create-engineer` skips the agent question.

### 4. Run it as a connected machine

On a Mac with the Papaya desktop app: point the app's working folder at this
checkout and click Connect. The app starts `ppy serve` here and keeps it running
while the app is open. Connect as the same engineer you set up in step 3.

Anywhere else, start it yourself and leave it running:

```bash
./bin/ppy serve
```

[Run it on a machine without the app](docs/operating.md#run-it-on-a-machine-without-the-app)
has the systemd unit, the WSL2 notes, and the one-serve-per-machine rules.

### 5. Hand it work

Assign a work item to your engineer in Papaya, or tell your machine what to do from
the app ("Have my machine add a health endpoint to the API"). It picks the ticket up,
chooses the repository, briefs a worker in an isolated worktree, reviews the exact
diff, opens the pull request, and reports on the ticket. Locally, `./bin/ppy status
--team` shows the whole team in one picture, and `./bin/ppy blockers` lists anything
it needs from you.

## Other ways to run it

- **An interactive session.** Open `claude` or `codex` in this checkout and talk to
  it. It is the same agent with the same control plane, and it works beside a running
  `ppy serve` or on its own. Useful for steering, reviewing, and one-off asks.
- **Without Papaya.** Not connected is a mode, not a blocker. Everything local works
  the same (register repositories, brief, dispatch, review, deliver). It says once that
  it is better with Papaya, and `./bin/ppy papaya connect` sets that up.
- **A server, or WSL2.** Same steps, with a device code for the sign-in. See
  [Run it on a machine without the app](docs/operating.md#run-it-on-a-machine-without-the-app).

## What it does

- **Goes and gets the work.** Every few minutes it asks Papaya which tickets are
  assigned to its agent, and it finds the repositories you actually have and offers
  to take them on, learning how each one builds and tests before the first dispatch.
- **Delegates to the cheapest worker that can do the job.** Bounded tasks, each in
  its own worktree, under a hard, code-enforced model ceiling you set. Claude driving
  with Codex workers, or the reverse, is fine.
- **Reviews before you do.** It answers routine worker questions from durable
  decisions, reads the exact diff that will be pushed, and follows the pull request
  until it merges: red CI, conflicts and reviewer comments go back to the worker.
- **Comes to you only for the calls that need a person.** Direction, scope beyond
  the ask, new credentials, destructive actions, spend ceilings, and merging.

## Going deeper

- [`docs/operating.md`](docs/operating.md): identity, repositories, gates and
  budgets, `ppy serve` round by round, self-reported issues, state, model routing,
  and keeping it running on a machine without the app.
- [`docs/runtime-contract.md`](docs/runtime-contract.md): the contract a session
  follows, from preflight to delivery.
- [`docs/task-lifecycle.md`](docs/task-lifecycle.md) and
  [`docs/brief-example.md`](docs/brief-example.md): a task from brief to pull request.
- [`docs/provider-capabilities.md`](docs/provider-capabilities.md): what each
  harness version can do, measured.

## Updating

```sh
./bin/ppy update
```

It fast-forwards this checkout, rebuilds the environment when the lockfile changed,
and tells you how to restart. A running `ppy serve` tells you when an update is
available.

## Working on the runtime itself

Building Papaya Agent Runtime (rather than using it) follows the development contract
in [`AGENTS.md`](AGENTS.md). A harness opened here defaults to runtime mode, so if
you're hacking on the framework, edit `src/`/tests/docs as usual or set `PPY_DEV=1`
to switch explicitly.

```bash
make hooks       # once per checkout: enable the secret-scanning pre-commit hook
make test        # hermetic tests (no provider spawning)
make lint        # ruff check
make fmt         # ruff format
make test-live   # opt-in: spawn real Claude/Codex probes
make probe       # re-run the provider capability matrix
```

`make hooks` sets `core.hooksPath` to `.githooks/`, whose `pre-commit` runs
[gitleaks](https://github.com/gitleaks/gitleaks) (`brew install gitleaks`) over the
staged changes and refuses a commit that adds a secret. A false positive goes in
`.gitleaks.toml` as a narrow allowlist entry with a comment saying why it is safe.

The version is a git tag, read back out of the checkout, and every merge to `main`
tags the next patch. See [`docs/versioning.md`](docs/versioning.md).
