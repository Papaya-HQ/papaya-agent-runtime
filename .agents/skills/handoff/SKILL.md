---
name: handoff
description: >-
  Save where you are and hand the user a pickup prompt so the next session — after a
  compaction, a closed terminal, a new day — resumes exactly here with nothing lost.
  Use when the user asks to hand off, wrap up, save state, pause, "give me a pickup
  prompt", or before context gets compacted; also proactively when a session has done
  a lot and the ledger is behind. Mechanics live in `ppy handoff`; this skill owns what
  to flush first and how to deliver the prompt. Always ends by returning the pickup
  prompt to the user, verbatim.
user-invocable: true
---

# handoff

Everything durable about a run already lives in `.ppy/state.db` and `.ppy/memory/`.
What does **not** survive a compaction or a new session is *your* working context:
what the user actually asked for, what you decided, what you were about to do next,
and what you're waiting on. This skill has one rule — **record state first, then hand
off** — and one deliverable: a short, paste-able pickup prompt. The todo ledger,
state, and memory are the record; `ppy handoff` projects them into a snapshot file
(`.ppy/memory/handoff.md`) and the prompt names that file rather than carrying it.

**Every invocation ends with the pickup prompt returned to the user, verbatim.** No
exceptions, no "nothing to hand off" — an empty ledger still gets a prompt.

## Procedure

1. **Record state.** Make the todo ledger true *right now* — it is the only place your
   intent survives, and it's what the pickup prompt and the session-start hook read:
   - `ppy todo list` — close what landed (`ppy todo done <id>`), drop what's moot.
   - `ppy todo add "<next step>" [--run <id>] [--task <id>]` for each thing you intended
     to do next, in order — include the user's phrasing of the objective and any
     constraint or decision they gave you verbally, as their own todos if need be.
   - `--blocked-on user[:what]|review|task:<id>|access` for anything waiting on someone;
     say what decision is needed.
   Drop newly durable facts where they belong (`preferences.md`, `relationships.md`,
   `repos/<name>/notes.md`) — see the `durable-memory` skill. `ppy board` shows you the
   result; `.ppy/memory/tasks.md` is generated from it, so don't edit that.
2. **Check on the team and build the prompt.** Run `./bin/ppy handoff`. It reads state
   and the ledger, pings the supervisor, runs the worker health check, writes the
   snapshot to `.ppy/memory/handoff.md` — the ledger, every open run and task, and a
   **"Known risks at handoff"** section covering anything that could go wrong for
   in-flight work while you're gone: supervisor down or hung, workers whose process is
   gone, workers that have gone quiet (possibly stuck), workers that never posted a
   plan, workers still running that will queue results/questions nobody answers, tasks
   idle on a question, finished work awaiting review, and an empty ledger while runs
   are open — and prints a short pickup prompt that points at the file. The risks also
   go to stderr (`--json` gives you everything as data).
3. **Return the pickup prompt.** Your reply is: one plain line; then, if
   there are risks, **say them plainly above the prompt** in your own words — the user
   decides whether to wait, answer a worker, or go ("Two workers are mid-task; they'll
   keep going, but task 9 is sitting on a question until you're back"); then the
   prompt from `ppy handoff` **verbatim in a fenced code block**; then nothing else.
   Don't summarize it, don't narrate what you wrote, don't read the ledger back — the
   prompt *is* the deliverable.

## On resume (the other side)

When a session starts with a pickup prompt, do what it says *before speaking*:

1. **Reconnect to the team.** `ppy supervisor status`; if it's down, start it in the
   background (`ppy supervisor serve`) — a daemon that died with the old terminal takes
   every in-flight worker with it. Then `ppy reconcile` (dead runners →
   `needs_recovery`) and `ppy health` (silent workers). Resume what dropped with
   `ppy resume <id>`; check a quiet worker's latest report with `ppy task <id>` and steer
   or resume it.
2. **Read the snapshot** the prompt names (`.ppy/memory/handoff.md`) — the ledger, open
   runs, and known risks as of the handoff.
3. **Load the ledger and memory** — `ppy todo list`, `ppy board`, then preferences,
   relationships, improvements, and each active repo's `notes.md`.
4. **Reconcile the snapshot against live state** — `ppy run <id>` per open run. The
   snapshot is a starting point; live state wins. Workers keep working between
   sessions; `ppy task <id>` shows each one's latest progress report.
4. Greet with where things stand, anything that went wrong in the gap, and the single
   most actionable thing — described in plain terms (what each piece of work *is*,
   not its plan label; the ledger's shorthand is for you, not for the greeting). The
   ledger stays the living record from here.

## Boundaries

- Nothing about a handoff stops work: workers in the supervisor daemon keep going.
  Don't interrupt or wait on them to "clean up" before handing off.
- Don't invent state. If a task's status surprises you, that's what reconciliation on
  resume is for — record what you *know* in the ledger, not what you assume.
- Never hand-edit `.ppy/memory/handoff.md`; it's regenerated on every `ppy handoff`.
- `.ppy/` is machine-local and gitignored, but never write secrets, tokens, or
  credentials into a todo.
