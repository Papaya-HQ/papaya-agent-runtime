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
5. **Papaya, if this machine is connected.** `ppy papaya status` says whether this
   machine is pinned to an agent. Connected, that is the session's identity. Not
   connected is a mode, not a blocker: run standalone, with everything local working
   the same, and say once the one line the runtime prints ("Running without Papaya.
   It's better with it: … https://trypapaya.ai") — unless `PPY_QUIET_INVITE=1` or
   `papaya.invite = false` silenced it. Do not start a sign-in or wait for one. If the
   user asks to connect, `ppy papaya connect` signs in, pins the machine and installs
   the harness plugin; their only step is clicking Approve in the browser.
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
