---
name: update-ppy
description: >-
  Update this Papaya Agent Runtime install to the latest code without losing work:
  wait for a good stopping point, save durable state, stop the supervisor so every
  worker is recorded stopped with its session kept, fast-forward the checkout, sync
  the environment, bring `ppy serve` and the heartbeat back up on the new build, and
  resume everything that was running. Use when the user runs /update-ppy, asks to
  update / upgrade / pull the latest runtime, or after a runtime pull request merged
  and this install should run it. Mechanics live in `ppy` (supervisor, handoff,
  status, resume); this skill owns the order and the judgment of when it is safe.
user-invocable: true
---

# update-ppy

Merged runtime code does nothing until this install runs it. A pull alone is not an
update: `ppy serve` and its supervisor keep executing the build they started with
(`.ppy/run/supervisor.json` names it), so the checkout says one thing and the running
runtime does another. And a restart at the wrong moment costs work: a gate killed
mid-run, a review turn cut off, a delivery half done. This skill makes the update one
deliberate move with nothing lost.

The order is fixed: **stopping point → save → stop → pull → sync → start → resume →
verify.** Say one line to the person before each step that changes anything.

## 1. Is there anything to update?

- `git -C <runtime root> fetch origin` and compare `HEAD` with `origin/main`, and the
  running build (`git_head` in `.ppy/run/supervisor.json`) with both.
- Nothing new and the running build is `HEAD`: say so and stop.
- The checkout must be on `main` and clean. Uncommitted or unpushed work in the
  runtime checkout is someone's; stop and ask rather than stash or discard it.
- Say what is coming in: `git log --oneline HEAD..origin/main`, in plain words.

## 2. Wait for a good stopping point

Read `ppy status --team` (each worker's current command and gate, each held ticket's
phase). The update waits while any of these is true, because a restart would lose or
repeat it:

- a gate or full suite is running under the supervisor (a worker's gate shows running);
- a manager turn is running on a held ticket (brief, answer, review, delivery);
- a `ppy deliver` or merge is in progress;
- a worker's latest tool call is a push or its scoped gate, started in the last few
  minutes.

A worker simply writing code is **not** a reason to wait: stopping records it
`worker_stopped` with its session, and it resumes where it was. Wait with a Monitor
until-loop on `ppy status --team`, never a sleep chain. Give it at most 20 minutes;
if something is still mid-gate after that, tell the person what and ask whether to
wait longer or go ahead (the gate reruns after the update).

## 3. Save durable state

Follow the `handoff` skill's first step: make the todo ledger true (a next step
recorded against every open task, with `--task <id>`), write newly durable facts to
memory, then run `./bin/ppy handoff` so `.ppy/memory/handoff.md` is current. Note the
tasks that are running right now; step 7 checks each one came back.

## 4. Stop

`./bin/ppy supervisor stop`. The supervisor asks every worker to stop, waits up to
`supervisor.stop_timeout` for each to be recorded stopped with its session kept, and
`ppy serve` releases the tickets it holds and exits. Stop the heartbeat monitor too
(it is running the old code). Never kill a supervisor or `serve` by hand; if it will
not stop, `ppy supervisor status` and `ppy blockers` say why, and that is the
person's to see.

## 5. Pull and sync

- `git -C <runtime root> pull --ff-only origin main`. A pull that is not a
  fast-forward means the checkout diverged: stop and ask.
- `./bin/ppy doctor` — the launcher syncs the environment for the new lockfile before
  it runs, and doctor reports the schema and config it migrated. A sync refusal or a
  failed migration is a blocker: say it with its words and stop.

## 6. Start

- **Hosted by the Papaya desktop app** (`serve --supervised`, the usual case): the
  app starts `ppy serve` again when it has stopped. Wait (Monitor until-loop, up to
  2 minutes) for `.ppy/run/supervisor.json` to show `git_head` equal to the new
  `HEAD`. If it does not come back, ask the person to restart the runtime from the
  Papaya app (or quit and reopen it). That is the one click this needs.
- **Run from a terminal** (`ppy serve` without `--supervised`, or no Papaya
  connection): start `./bin/ppy serve` again in the background the way it was run.
- Start the heartbeat again: `./bin/ppy watch --follow` as a background monitor, then
  `./bin/ppy papaya tools --check` (new code may have moved the client's interpreter;
  `ppy papaya tools` fixes it, and the session loads it after `/mcp`).

## 7. Resume and verify

- The new `serve`'s first round resumes each stopped worker from its session. Check
  every task noted in step 3 with `ppy status --team`: back `in_progress`, or finished.
  One still `worker_stopped` after the first round: `ppy resume <id>`. A task whose
  lease was released cannot be resumed; send its archived brief out again
  (`.ppy/briefs/<repo>/task-<id>.md`, same `--run-id`) and close the old one.
- Held tickets: a ticket marked `handed_over` "held by" this runtime's own agent is
  the restart losing its own hold, not a handover. Take its work up by hand (review,
  deliver, report on the item) and record a todo against the task.
- `ppy readiness` and `ppy blockers`: anything new since the update is the update's
  to explain.
- Tell the person, in plain terms: the build it is running now (short commit), what
  changed, which workers resumed, and anything that did not come back.

## Boundaries

- Only the runtime checkout is updated. Registered repositories' base clones are
  `ppy repo sync`'s business, not this skill's.
- Never stash, reset or discard local changes in the runtime checkout to make a pull
  work.
- Never kill processes by hand, never run the launcher's environment sync directly,
  and never restart the Papaya app yourself.
- An update is not a reason to close, re-dispatch or re-brief work that resumes on
  its own.
