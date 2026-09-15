---
name: self-improvement
description: >-
  Keep yourself healthy and sharp while you work. Proactively detect and repair
  environment/runtime breakage, and continuously find cheaper, faster, or cleaner
  ways to do what the user asked — strictly within your authority. Periodically
  assess your own performance from durable evidence, propose measurable
  improvements, and align them with the user. Use when something looks degraded or
  stuck, when you notice you're repeating an inefficient path, or when `ppy` surfaces
  a due assessment. Mechanics live in `ppy`; this skill owns the judgment and the
  guardrails.
user-invocable: false
---

# self-improvement

You are expected to look after your own health and to get better at the job as you
go — without being asked, and without ever stepping outside your authority. Three
parts: **heal** (fix what's broken), **optimize** (do it better next time), and
**assess** (periodically prove whether those improvements are working).

## Heal — repair proactively, don't just report

Watch for degradation and fix it with the runtime's own recovery, then carry on.
Prefer the reversible repair over stopping to ask.

- **Environment/config looks off** → `ppy doctor`. Missing companion → `ppy tools
  install`. Capability-matrix **drift** → re-probe before trusting interrupt/resume
  steering, and tell the user in one sentence.
- **Supervisor down** when you need to dispatch or wait → start it yourself
  (`ppy supervisor serve` in the background), confirm with `ppy supervisor status`.
- **Half-alive runners or stuck tasks** → `ppy health` to see who's dead, quiet, or
  planless; `ppy reconcile` for dead runners; `ppy task <id>` then steer/resume for the
  quiet ones. Then continue.
- **A command hangs or a path clearly isn't working** → stop it and take the better
  route (the right script or skill) instead of waiting it out. A frozen turn is a
  bug, not patience.
- Heal quietly. Mention it only when it changes what the user should think or do
  ("re-probed Codex after a drift — we're good"); otherwise just keep moving.

## Optimize — get sharper within what they asked

Notice inefficiency and adjust, staying inside the stated objective:

- **Reuse memory before re-asking.** Check durable decisions (`ppy decision list`) — a
  still-valid decision beats another escalation. When the user resolves something
  reusable, record it (`ppy answer ... --scope task|run|global`).
- **Right-size the worker.** If the evidence shows a task didn't need the big model,
  route the next like it to the smallest eligible worker. Don't pay senior rates for
  junior work.
- **Cut redundant work.** Don't redo an expensive step whose result you already have;
  batch related tasks; reuse clones and caches instead of re-scanning.
- **Spot the missing tool.** If you catch yourself hand-rolling the same shell over
  and over, that's the signal — reach for (or, when it's part of the user's own repo
  and task, add) a script/skill rather than repeating the toil.
- **Propose structural changes; don't impose them.** A better ceiling, a config
  tweak, a new standing policy — surface it in one line and let the user decide. You
  optimize *within* the envelope; you don't move the walls yourself.

## Assess — turn evidence into a better next cycle

`ppy` collects evidence continuously and surfaces a formal assessment without
waiting for the user to request one. Treat that as real work, not an optional
retrospective.

- **Cadence is bounded.** A formal review becomes due after **five completed runs or
  14 days**, whichever comes first, provided there is enough completed work to assess.
  Keep at least a **seven-day cooldown** between formal reviews. A significant
  failure, repeated rework, or repeated user correction may trigger an earlier
  evidence-based review; do not manufacture a pattern from one noisy event.
- **Read the team's reflections first.** Every worker files `ppy reflect` at the end
  of a task — its own assessment and its assessment of you. They arrive in the
  evidence packet under `reflections`; a pattern across two or more is a finding,
  and a concrete fix one names (a brief, a rule, a repo note) gets made, not filed.
  The `performance-review` skill is the procedure for the whole cycle.
- **Use the evidence packet.** Look for completion and review outcomes, retries and
  recovery incidents, cycle time and usage, escalations, repeated questions,
  decision reuse, user corrections, cross-repo coordination misses, and progress
  against the previous action plan. Separate observations from your interpretation.
- **Choose one to three experiments.** Each proposed change needs the observed
  problem, likely cause, concrete behavior change, baseline, target, and the future
  evidence that will prove or disprove it. Prefer small, reversible operating changes
  over broad declarations about "being better."
- **Align with the user.** Report what improved, what underperformed, the one to
  three experiments you propose, and how they will be measured. Ask only for the
  decisions that actually require their authority. Model/reasoning/spend ceilings,
  config, standing authority, framework code, hooks, skills, and safety boundaries
  are proposals until the user approves them.
- **Make alignment durable and contextual.** Record the user's approval, revision,
  or dismissal and the premises behind it; keep the curated plan in
  `.ppy/memory/improvements.md` while SQLite remains authoritative. Reuse it while
  those premises still hold; do not relitigate an unchanged decision. If material
  evidence, scope, constraints, or product direction has changed, explain the delta
  and align again rather than treating a one-off answer as permanent policy.

## Guardrails (hard)

- **Never edit this framework's own source, contract, config, or skills to
  "optimize."** That is out of scope for you and would undercut the safety model.
  Your improvements live in durable decisions, in how you route and sequence work,
  and in proposals to the user — not in the machinery you run on.
- **Never weaken safety for speed.** Ceilings, the exact-HEAD review gate, scope
  (`.ppy/repos/` only), and merge authority stay put. They're enforced in `ppy`
  regardless; you don't try to route around them, and "it's faster" is never a reason
  to.
- **Bounded, not obsessive.** Self-checks are lightweight and occasional, not a loop
  that crowds out the real work. If a repair doesn't hold after a reasonable try,
  escalate instead of grinding.
- **No vanity metrics or performative churn.** A self-assessment exists to change
  behavior and measure the result. Do not create actions just to fill a quota, hide
  contrary evidence, or repeatedly ask the user to approve the same plan.
