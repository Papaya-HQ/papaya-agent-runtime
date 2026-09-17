---
name: onboard-a-repo
description: >-
  Bring a repository under management properly — find it, register it with a forge,
  learn how it builds, tests and gates, and close the unknowns before the first
  worker is dispatched. Use when the user wants more of their code worked on, when
  they name a repository the runtime does not have, when a repository is registered,
  and whenever a brief needs a fact about a repository nobody has written down.
  Mechanics live in `ppy repo discover` / `add` / `onboard`; this skill owns the
  judgment — what to offer, what to ask, and what not to guess.
user-invocable: true
---

# onboard-a-repo

The first dispatch into a repository nobody has read is a worker guessing at the test
command, the lint gate and the branch conventions — guessing in a worktree, slowly,
on the clock. Onboarding is how that never happens twice. It is cheap, it is offline,
and it is the difference between a brief that names the real CI gate and one that
names a local approximation of it.

## Finding a repository to take on

Run `ppy repo discover`. It reads the forge — the user's own account and every
organization they belong to — and lists what is not registered yet, most recently
pushed first, archived repositories and forks excluded.

- **Offer, don't register.** Registration is what makes a repository something a
  worker may change, so it stays the user's call. Name two or three that look live,
  in a sentence, and let them pick. Never a table.
- **Read the forge, never the filesystem.** Do not scan `~`, `~/workspace`, or
  anywhere else on the machine, and never guess a local path. A repository sitting on
  disk that nobody mentioned is not a signal.
- **A repository the user names is never a dead end.** If discovery knows it, offer to
  register it right then and do it on a yes. If it is outside their organizations, ask
  for the URL. "I don't have that repo" is not an answer on its own.
- **Offer at the right moment.** When nothing is registered at preflight. When they
  describe work that clearly lives somewhere you do not have. When they ask what you
  can work on. Not repeatedly, and not as a list of everything.

## Registering it

`ppy repo add <url-or-path> [--forge-url <url>] [--name <name>]`.

Registration records the **forge** — where this repository's pull requests get opened.
A URL registration is its own forge. A path registration inherits the forge from that
checkout's `origin`, and if that origin is another local clone, `ppy repo add` refuses
until `--forge-url` names one. Ask the user for the GitHub URL rather than registering
a repository whose delivery would have nowhere to go; a repo with no forge strands
every delivery, and `ppy doctor` will keep flagging it.

## Learning it

`ppy repo onboard <name>` reads the base clone and records, into that repository's
durable notes:

- **how it builds and verifies** — the test, lint, typecheck and build commands, with
  the right runner (`pnpm` vs `npm run`, `uv run pytest` vs `pytest`);
- **what CI actually runs** — the literal commands in the workflow files, which is what
  a brief's verification suite has to be;
- **the contracts it carries** — `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `CODEOWNERS`;
- **whether UI work has a design reference** to match rather than invent.

Hand-written notes outside the onboarding block survive a re-run, so re-onboard freely
after a repository changes its build.

## Closing the unknowns — the part that is yours

Onboarding ends with a list of what it could not determine. **That list is the work.**
An unknown that gets named gets asked about; an unknown that stays silent gets guessed
at by a worker at full model price.

For each one:

- **No test command found.** Onboarding already read the instructions, pull-request
  CI, the Makefile and package scripts, and the push hooks. Read what it cannot — a
  `justfile`, a `docker-compose` test service, a README section, the CI job a workflow
  calls — and record what you find with `ppy repo set`. The user is never the first
  place to look: ask for the one command that proves a change is good only when the
  repository genuinely does not show it anywhere. One question, not a questionnaire.
- **No CI workflow commands found.** Say plainly that a green local run is not proof of
  a gate, and find out what the gate is before briefing work that has to pass one.
- **No stated conventions.** Read the recent commit history for branch naming, commit
  style and PR shape, and write what you find into the notes yourself.
- **Stack not recognised.** Look at what is actually in the tree before assuming.

Record every answer in the repository's notes. The point of onboarding is that nobody
— you, a worker, or the next session — ever has to ask it twice.

## Before the first dispatch

The repository is ready when a brief can name, from the notes and without asking:

- the exact starting commit (`ppy repo sync <name>` first, so it is the remote tip);
- one authoritative verification suite that is the CI gate, not a local stand-in;
- the design reference, if the work touches UI;
- any convention a reviewer would otherwise send the work back for.

If any of those is still missing, it is a question for the user now, not a rejected
diff later. See the `brief-a-worker` skill for what the brief itself has to satisfy.

## Telling the workspace

When you have taken on a new set of repositories and the user is connected to Papaya,
it is worth saying in the workspace which repositories you can build in — so their
teammates can point work at you directly instead of going through them. Say it once,
where it belongs, with the repositories named. Do not announce every registration.
