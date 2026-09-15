# Papaya Agent Runtime initial implementation plan

This is the milestone-gated implementation plan derived from the approved
"Probe-first Papaya Agent Runtime implementation" plan. Code edits stay small and
gated by the current milestone. After each milestone: run `make lint`,
`make fmt`, `make test`; create a checkpoint commit whose message records what
landed and what drifted; re-read this plan; then wait for human approval before the
next milestone. There is no checked-in task log — the record of a change is its
commit message and pull request body.

The [`design/papaya-agent-runtime-plan.html`](../design/papaya-agent-runtime-plan.html) surface
records the seven decisions accepted on 2026-08-28; do not reopen them.

## Why probe-first

The technical plan names interrupted provider turns and half-alive processes as
the largest technical uncertainty and asks to re-estimate after real resume and
interruption probes. So the first code measures the installed CLIs rather than
building the kernel top-down. This is a sequencing choice only; the kernel,
supervisor, adapters, and delivery layers remain exactly as designed.

Interrupt steering is capability-gated everywhere downstream: until a
provider/version proves mid-flight resume, adapters expose only
checkpoint-at-completion steering plus a compact fresh-session recovery packet.

## Milestones

### M0 — Live Claude/Codex interrupt and resume

First code. Measure the installed CLIs; build no supervisor, Treehouse, setup
wizard, or real adapters yet.

Deliverables:

- `pyproject.toml`, `Makefile`, Ruff + pytest, `src/papaya_agent_runtime/` package
  stub, zero runtime dependencies.
- `schemas/provider-capability.schema.json` — fail-closed capability schema.
- `src/papaya_agent_runtime/probes/` — isolated-fixture probe harness that spawns real
  `claude -p` and `codex exec`, applies interrupts, attempts resume, and records
  a fail-closed matrix.
- `docs/provider-capabilities.md` — recorded matrix for the installed versions.
- Hermetic unit tests plus an opt-in `-m live` probe test (`make test-live`).

Probe matrix: clean complete + resume; SIGINT during a model turn; SIGINT during
a long tool command (with orphan/dirty observation); resume from a worktree and
from the main checkout; duplicate/late resume; mid-process steer attempt;
session-file survival across clean exit / SIGINT / SIGKILL; session id and usage
presence in the stream.

Acceptance: both installed CLIs have a recorded capability row; default tests
stay hermetic; `make test-live` documented and opt-in; checkpoint commit and
drift review exist; no later milestone started.

### M1 — Kernel foundation (after approval)

Idempotent `bin/install`, shared setup skill, task-oriented `README.md`, harness
discovery, `ppy setup` / `ppy config` / `ppy doctor` stubs, SQLite schema,
`ppy repo add`, config validation. No live workers.

### M2 — Kernel runtime (after approval)

Supervisor Unix socket, per-task runner guardian, append-only spool, fake
provider, Treehouse lease lifecycle, `ppy wait --until-actionable`,
`ppy reconcile`. Lands the technical-plan Phase 1 acceptance, including a
feedback-capable artifact and run reconcile.

### M3 — Real workers (after approval)

Claude and Codex adapters behind `probe/start/resume/steer/interrupt/reconcile/
parse_event/parse_usage/result`, consuming the M0 capability flags. Task/result
schemas, usage accounting, ceiling enforcement, checkpoint steer, blocked-worker
resume. Mixed-provider fixture task.

### M4 — Autonomous completion (after approval)

Lifecycle hooks, question routing, durable scoped decisions, Lavish feedback
loop, exact-HEAD review gate, `gh-axi` push/PR, cross-repo dependency checks.

### M5 — Hardening (after approval)

Crash/replay/CLI-drift tests, memory controls, usage reports, doctor polish,
docs. Re-estimate delivery after M0 evidence, per the technical plan.

### Approved cross-cutting addition — proactive self-assessment

The user explicitly authorized this addition on 2026-08-29. It spans M4's
autonomous manager loop and M5's durable-state/hardening work without advancing
either milestone gate or expanding the manager's authority:

- collect a deterministic evidence packet from durable run, task, review, usage,
  decision, recovery, escalation, and user-correction state;
- persist assessment cycles and their one-to-three measurable experiments in
  SQLite, including the user's aligned/revised/dismissed outcome, with a curated
  summary under `.ppy/memory/improvements.md` for session recall;
- make a formal review due after five completed runs or 14 days, whichever comes
  first, subject to minimum evidence and a seven-day cooldown; surface significant
  failures, repeated rework, or repeated user correction for earlier review;
- deliver due work through the supervisor and Claude/Codex lifecycle hooks so the
  manager initiates the review rather than waiting for the user;
- expose `ppy assessment tick|status|show|complete|align` for deterministic,
  recoverable mechanics; and
- keep structural changes to models, ceilings, config, standing authority,
  framework code, hooks, skills, and safety boundaries as proposals that require
  user alignment. Reuse aligned decisions until their premises materially change.

Acceptance: cadence and failure triggers are deterministic; a due assessment
survives restart; hook delivery cannot loop; the manager produces no more than
three measurable experiments; user alignment is durable and context-sensitive;
and existing authority/review gates remain unchanged.

## Guardrails

- Small diffs gated by the current milestone.
- Prefer the planned tree: `src/papaya_agent_runtime/`, `schemas/`, `templates/`,
  `tests/`. No Firstmate source copies, no vendored companion trees, no tmux in
  the protocol.
- Use `uv` for all Python commands.
- Record milestone state and drift in the checkpoint commit message as each lands,
  never in a checked-in tracker.
