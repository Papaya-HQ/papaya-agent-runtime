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

1. Run `./bin/install`. It verifies Git, uv, Python 3.12+, and warns about Node
   and `gh`. It is idempotent and safe to rerun after a partial failure.
2. Run `ppy doctor` (or `ppy doctor --json`). Read the harness, requirement, and
   companion-tool sections.
3. If a harness is installed but unauthenticated, hand the exact login step to
   the user: `claude auth login` or `codex login`. Never authenticate on their
   behalf.
4. When at least one harness is authenticated, run `ppy setup`. Offer only the
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
5. **Connect to Papaya.** `ppy papaya status` says whether this machine is pinned to
   an agent. If it is not, run `ppy papaya connect` yourself — it signs in, pins the
   machine, and installs the harness plugin that carries the `papaya` MCP server and
   the rule-enforcing hooks. The user's only step is clicking Approve in the browser,
   so say that in one sentence and wait; do not hand them the command.

   This is the step that gives the session an identity, so it is worth doing, but it
   is **never a prerequisite**. If they decline, if the network is down, or if connect
   fails, say once what is unavailable without it (work items, channels, workspace
   memory) and continue. A runtime that refuses to build code because a workspace is
   unreachable is worse than one that builds code quietly.
6. Register work with `ppy repo add <url-or-path>`. If the user does not name one,
   run `ppy repo discover` and offer what it finds rather than asking an open
   question. Then `ppy repo onboard <name>` each newly registered repo — see the
   `onboard-a-repo` skill — and confirm with `ppy repo list` and `ppy status`.

## Repair

- Missing requirement: name it, explain what installs it, and ask before running
  anything that changes the system.
- Invalid config: `ppy doctor` reports the validation error. Re-run `ppy setup` or
  `ppy config models` / `ppy config authority` to fix the specific field.
- A harness that disappeared or lost auth: `ppy doctor` shows it as unavailable.
  Guide re-authentication rather than silently switching providers.

## Boundaries

- Deterministic probes and state changes belong to `ppy`, not to shell commands
  you type by hand.
- Do not enable dangerous permission-bypass modes.
- Merge authority stays off unless the user explicitly grants it via
  `ppy config authority --allow-merge`.
