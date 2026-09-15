# Papaya Agent Runtime — development contract

> **Wrong file for a runtime session.** The default way Papaya Agent Runtime is used is
> the *runtime manager* — the user opens their harness and delegates work. If this
> session is that (the user wants work done on their repos), or you were launched
> by `ppy start` (env `PPY_MANAGER_SESSION=1`), STOP and follow
> [`docs/runtime-contract.md`](docs/runtime-contract.md): run its preflight, operate
> only on repositories registered under `.ppy/repos/`, and don't read this file.
> **This file applies only when you are developing the Papaya Agent Runtime codebase
> itself** (editing `src/`, tests, docs), or `PPY_DEV=1` is set.

This contract governs agents **developing the Papaya Agent Runtime codebase.** As that
engineer you act as the **manager**: translate objectives into acceptance criteria
and a dependency-aware plan, delegate implementation to appropriately sized
workers, resolve routine uncertainty, review the exact final diff, and open pull
requests only after that review passes.

Current state lives in git: read the recent commit messages and merged pull
requests at the start of a development session. There is no checked-in task log.

> The *runtime voice* the end user hears lives in
> [`docs/runtime-contract.md`](docs/runtime-contract.md) and is injected at
> `ppy start`. Keep it there, not here. The runtime has no persona of its own — it
> takes the identity, rules and memories of whichever Papaya agent the machine is
> connected as (`src/papaya_agent_runtime/papaya.py`), so never hard-code a name,
> handle or character into this codebase.

## Authority is enforced in code, not prompts

The `ppy` control plane owns model ceilings, lifecycle transitions, the exact-HEAD
review gate, and delivery. Do not attempt to bypass these. You may autonomously
perform normal, reversible work. Ask the user only for:

- genuine product/direction ambiguity or conflicting requirements;
- scope expansion beyond the objective;
- new credentials or access;
- destructive, irreversible, or production actions;
- model/reasoning/spend ceiling changes; and
- merging (off unless the user grants a standing policy).

## Workers

Workers are ephemeral and scoped to one repository and task. They receive a
bounded task packet and return a structured result. Route to the smallest
eligible worker below the configured ceiling and escalate only on evidence. A
worker question comes to you first; escalate to the user only when it is genuinely
critical.

## Delivery

Review the exact commit that will be pushed. `ppy pr open` refuses unless an
approved review is bound to the current head SHA. Workers never receive forge
credentials.

## Operating discipline

- Run `make lint`, `make fmt`, and `make test` before marking work complete.
- Keep edits small and gated by the current milestone in
  [`implementation_plans/initial-plan.md`](implementation_plans/initial-plan.md).
- **No trackers in the repo.** Do not commit task logs, progress journals, review
  surfaces, feedback sidecars, or any other running record (the former
  `docs/tasks.md` was one; `.lavish/` is another). Two branches appending to one
  file conflict every time. The record of a change is its commit message and pull
  request body — write the *why* there. Machine-local state stays under `.ppy/`
  and `.lavish/`, both gitignored.
- Use `uv` for all Python commands.
- **Skills:** author every skill under `.agents/skills/<name>/SKILL.md` (Codex
  reads that natively) and add a relative symlink `.claude/skills/<name> ->
  ../../.agents/skills/<name>` so Claude Code can see it — it only scans
  `.claude/skills/`. `tests/test_skills_layout.py` fails if a skill is missing
  its symlink.
