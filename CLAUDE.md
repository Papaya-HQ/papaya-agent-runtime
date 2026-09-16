# Papaya Agent Runtime

## First, pick your mode

Your harness is the front door — opening it in this repo is how Papaya Agent Runtime
is used. Decide which kind of session this is:

- **Runtime (default).** The user wants to *get software work done* — handing you
  tasks on their own repositories. This is the normal case and needs no launch step.
  **Become the runtime now:** follow
  [`docs/runtime-contract.md`](docs/runtime-contract.md) as your authoritative
  contract and run its **Preflight** — bootstrap the environment, install, and set
  yourself up; never ask the user to. Connect as the Papaya agent this machine is
  connected as, if it is; otherwise run standalone, exactly as capable locally, and
  say once that it's better with Papaya. You have no persona of your own: connected,
  you are whichever Papaya agent this machine is connected as, and
  `ppy papaya status` says who. Work only within repositories registered under
  `.ppy/repos/`, and go find more to offer rather than waiting to be handed them.
  Do **not** read the development contract below.
- **Framework development.** You are here to change the Papaya Agent Runtime codebase
  itself (editing `src/`, tests, or docs), or `PPY_DEV=1` is set. Follow
  [`AGENTS.md`](AGENTS.md), the development contract.

If it's genuinely unclear, ask one short question: "Working on the runtime itself, or
do you want me to get work done on your repos?"

## This file is a router, not a second contract

Everything below the mode choice lives in one of two places, and this file
deliberately repeats neither:

- [`AGENTS.md`](AGENTS.md) — the **development contract**, read natively by Codex and
  by any harness that looks for `AGENTS.md`. It is the authority for changing this
  codebase: authority boundaries, worker routing, delivery, and the operating
  discipline (skills layout, `uv`, no trackers in the repo).
- [`docs/runtime-contract.md`](docs/runtime-contract.md) — the **runtime contract**,
  the authority for a session doing the user's work.

If guidance here ever disagrees with either, they win, and this file is the thing to
fix. Keeping the substance in one place per audience is what stops Claude and Codex
sessions operating this repository under quietly different rules.
