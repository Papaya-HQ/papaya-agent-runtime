---
name: setup-runtime
description: >-
  Shared first-run setup and repair guidance for Papaya Agent Runtime, visible to both
  Codex and Claude. Use when installing, configuring, diagnosing, or repairing a
  Papaya Agent Runtime agent home. The deterministic install/version/auth probes live
  in `ppy`; this skill owns guidance and judgment, not shell mechanics.
user-invocable: true
---

# setup-runtime

Papaya Agent Runtime separates mechanics from judgment. The `ppy` control plane owns the
deterministic install, version, and auth probes. This skill interprets their
output, explains exactly what will change, asks for approval when required, and
hands authentication steps back to the user.

## First run

A person at a terminal has one command: `./bin/ppy setup`, then `./bin/ppy serve`. It
checks the machine, runs the Claude Code and GitHub sign-ins on their own terminal,
connects to Papaya and opens a repository picker, saying one line for each step that is
already done; `./bin/ppy setup --repos` reopens the picker. Point them at it when they
ask how to set a machine up. A session never runs the guided form itself (its
sign-ins need the person's terminal); the steps below are the session's own path.

1. Run `./bin/install`. It verifies Git, uv and a `python3`, and warns about Node
   and `gh`; the interpreter the runtime runs on is the series `.python-version`
   pins (3.13), which uv fetches when it is missing. It is idempotent and safe to
   rerun after a partial failure. Linux and macOS run natively; Windows only inside
   WSL2 (the runtime needs `fcntl`). For a machine with no desktop app — a server,
   SSH, WSL2 — the whole procedure, including keeping `ppy serve` running, is the
   `docs/operating.md` section "Run it on a machine without the app"; follow and link it, don't restate it.
2. Run `ppy doctor` (or `ppy doctor --json`). Read the harness, requirement, and
   companion-tool sections.
3. If a harness is installed but unauthenticated, hand the exact login step to
   the user: `claude auth login` or `codex login`. Never authenticate on their
   behalf.
4. When at least one harness is authenticated, run
   `ppy setup --profile-only --non-interactive` with the profile flags. Offer only the
   harnesses `ppy doctor` reports as usable. Setup collects the driver profile
   and the hard worker ceiling and writes `.ppy/config.toml`.

   Both roles default to the **same** harness, and it is never picked by list
   position: the one this machine is connected to Papaya with (`ppy papaya status`),
   or — with no connection yet — whichever one drives. A Claude connection means
   Claude workers; a Codex connection means Codex workers. Mixing is still allowed,
   but only when someone asks for it: an explicit answer at the prompt,
   `ppy config models --worker-provider ...`, or `ppy dispatch --provider ...` for
   one task. If a configured harness later stops being usable, `ppy readiness` says
   so and names the sign-in step; do not switch the other harness in for them.
5. **Papaya, if this machine is connected.** `ppy papaya status` says whether this
   machine is pinned to an agent. Connected, that is the session's identity. Not
   connected is a mode, not a blocker: run standalone, with everything local working
   the same, and say once the one line the runtime prints ("Running without Papaya.
   It's better with it: … `ppy papaya connect` sets it up … https://trypapaya.ai") —
   unless `PPY_QUIET_INVITE=1` or `papaya.invite = false` silenced it — offering to do
   it for them. Never wait on it. If they say yes, run `ppy papaya connect --create-engineer` yourself (their own engineering agent, created when they have none; `--agent` instead only when they name another)
   with a timeout of at least ten minutes: it installs the client when there is none
   (`npx papaya-agent`, or `uv` on a machine without Node), opens a sign-in link, pins
   the machine and installs the harness plugin; their only step is clicking Approve.
   Relay the link it prints; on a machine with no browser, add `--device` so they
   approve a code from another device, choosing the workspace and agent in the Papaya
   app as they approve (`--workspace`/`--agent` are ignored with `--device`). If the
   browser flow exits listing several workspaces or agents, ask the
   person which in the conversation and re-run with `--workspace`/`--agent`; if it says
   there is neither Node nor `uv`, tell them which to install. Then `ppy papaya tools`
   and `/mcp`.
6. Register work with `ppy repo add <url-or-path>`. If the user does not name one,
   run `ppy repo discover` and offer what it finds rather than asking an open
   question. Then `ppy repo onboard <name>` each newly registered repo — see the
   `onboard-a-repo` skill — and confirm with `ppy repo list` and `ppy status`.

## Repair

- Missing requirement: name it, explain what installs it, and ask before running
  anything that changes the system.
- Invalid config: `ppy doctor` reports the validation error. Re-run
  `ppy setup --profile-only --non-interactive` or
  `ppy config models` / `ppy config authority` to fix the specific field.
- A harness that disappeared or lost auth: `ppy doctor` shows it as unavailable.
  Guide re-authentication rather than silently switching providers.

## Boundaries

- Deterministic probes and state changes belong to `ppy`, not to shell commands
  you type by hand.
- Do not enable dangerous permission-bypass modes.
- Merge authority stays off unless the user explicitly grants it via
  `ppy config authority --allow-merge`.
