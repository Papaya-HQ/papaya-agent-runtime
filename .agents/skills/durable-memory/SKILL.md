---
name: durable-memory
description: >-
  Persist and recall what you learn so you never relearn a repo, a cross-repo
  relationship, or the user's preferences across sessions. Use at the start of every
  session to load context, and whenever you learn something durable while working.
  The store is machine-local Markdown under `.ppy/memory/`; the authoritative state is
  `.ppy/state.db`. This skill owns the judgment — what's worth remembering and how to
  keep it curated.
user-invocable: false
---

# durable-memory

You keep a durable memory for this instance so each session starts smart, not blank.
It lives under `.ppy/memory/` (gitignored, machine-local) in **two tiers, split by who
owns them**:

**Per-instance (this `ppy`) — your layer, spanning every repo.** Cross-repo knowledge:

- `preferences.md` — how the user likes things done everywhere (comms, PRs,
  engineering defaults, standing decisions).
- `relationships.md` — how the registered repos relate (dependencies, shared
  contracts, rollout order).
- `improvements.md` — the curated performance experiments aligned with the user and
  what happened when you tried them. Detailed assessment state remains in SQLite.
- `tasks.md` — the work board, **generated** by `ppy board` from the todo ledger and live
  task state. Never hand-edit it; change it through the ledger (below).

**Per-repo — the shared worker workspace, under `repos/<name>/`.** Auto-seeded when a
repo is registered; **workers** read and write it as they work:

- `notes.md` — what the repo is, build/test/run commands, conventions, gotchas,
  key areas. Workers consult it and extend it.
- `tasks.md` — durable **follow-ups** for the repo: backlog, tech debt, known issues.
  Live progress is *not* logged here (see below).

Resolve paths with `ppy memory path [--repo <name>]`; `ppy memory show [--repo <name>]`
prints them (the repo view starts with the progress log rendered from state); `ppy memory
init` scaffolds the instance tier and seeds a directory per registered repo.

## Intent and progress are state, not prose

- **Your intent → the todo ledger.** `ppy todo add "<next step>" [--run <id>] [--task <id>]
  [--blocked-on user[:what]|review|task:<id>|access]`, `ppy todo done <id>`, `ppy todo
  block <id> --on …`, `ppy todo list`. Record a next step the moment you know it and close
  it when it lands. `ppy status`, `ppy board`, `ppy handoff`, and the session-start hook all
  read the ledger, so it is what survives compaction — and with work open and no todo
  recorded, `ppy` refuses to let you stop until you write one.
- **Worker progress → `ppy progress`.** Every dispatched worker is told to report
  `ppy progress <task_id> --phase plan --note "<approach>"` before implementing, then
  `implement|test|review|blocked|done` as it goes. To check on a worker, read
  `ppy task <id>` (latest report) or `ppy memory show --repo <name>` (the log) rather than
  asking — the narrative is already there.
- **Review the plan early; steer if it's wrong.** Read the plan report while course is
  still cheap to change: if the approach is off, `ppy steer <task_id> --message ...` (or
  `ppy resume <task_id> --message ...`) — don't wait to reject the finished diff. The
  supervisor flags `plan_missing` (no plan past the grace period) and `worker_quiet`
  (silent past the threshold) as actionable events; `ppy health` shows the team on demand.

## Recall — load before you work

- **At session start**, read the per-instance tier (`preferences.md`,
  `relationships.md`, `tasks.md`, and `improvements.md`). When the user names a repo,
  also read its `repos/<name>/` (`notes.md`, and `tasks.md` if relevant) before doing
  anything on it. Treat these as ground truth — don't re-derive what's already
  written.
- **Before asking the user**, check memory (and durable decisions, `ppy decision
  list`). If the answer is on file, use it; don't make them repeat themselves.

## Record — capture as you learn

Write it down the moment it's durable, not at the end. Keep entries short and factual.

- **Your work tracking** → the todo ledger (`ppy todo`), never a file. The board is
  rendered from it.
- **Repo facts** → `repos/<name>/notes.md`: the real build/test/lint/run commands,
  the architecture in a line, conventions, and the gotchas that bit you this session.
- **Repo follow-ups** → `repos/<name>/tasks.md`: durable backlog and tech debt for
  that repo. Progress itself is `ppy progress` (state), not this file.
- **Cross-repo facts** → `relationships.md`: a dependency you discovered, a shared
  contract, the order changes must roll out.
- **Preferences (all repos)** → `preferences.md`: how the user wants updates, PRs,
  branches, reviewers, defaults — anything told once that should hold next time.
  Repo-specific conventions go in that repo's `notes.md`, not here.
- **Aligned performance experiments** → `improvements.md`: the active behavior
  changes, their targets, and outcomes. Keep this as a concise session-recall view;
  assessment cycles and action status in `.ppy/state.db` are authoritative.
- **Reusable decisions** → also record via `ppy answer ... --scope run|global` so the
  runtime reuses them automatically; summarize durable global ones in `preferences.md`.

## Curate — keep it trustworthy

- **Update in place; prune the stale.** Memory is a living cheat-sheet, not an
  append-only log. Wrong or outdated notes are worse than none — fix them when you
  notice.
- **Facts and preferences, not transcripts.** Don't paste large output or narrate;
  capture the durable takeaway.
- **Live task/run state, intent, and progress are not memory.** They live in
  `.ppy/state.db` (`ppy status`, `ppy todo`, `ppy progress`, decisions). Memory is for what
  should outlive the run.

## Boundaries

- Memory is machine-local and gitignored — never commit it, and never write secrets
  or credentials into it.
- It records the user's world (their repos, their preferences), never edits to this
  framework's own source, contract, or skills.
