# Task status, steering, and resume

A task's status is a claim about what is happening right now. It has to be true:
the manager schedules, nags, and reports off it, and the heartbeat treats
`failed` as an incident. This is the contract.

## Statuses

| Status | Meaning |
| --- | --- |
| `requested` | The task row exists; no worker has started. |
| `in_progress` | A worker session is live, or is being started right now. |
| `worker_done` | The worker finished a turn and reached its stored terminal phase. A `done` task also has nothing unpushed; a `review` task stays local for manager review and delivery. Neither may leave a backgrounded command hanging. |
| `worker_stopped` | The turn ended mid-gate. Not terminal, not a failure — the work is there and unfinished, and `ppy resume` picks it up. |
| `blocked` | The worker asked a question and stopped. |
| `failed` | The worker's *current* session ended without a result. |
| `needs_recovery` | The runner process vanished without recording a result. |
| `delivered` | Pushed (and normally a PR opened) after an approved review. |

## A turn ending is not the same as finishing

A Claude worker started the full backend suite, the tool's foreground cap pushed
it into the background, and the worker ended its turn to wait for it. The
backgrounded suite was killed with the session, the branch was never pushed, no
`done` note was ever filed — and the harness recorded `worker_done` (task 103,
2026-09-04; task 104 repeated it with browser smoke retries).

`ppy dispatch --ends-at review|done` records the task's terminal phase (`done` by
default). `ppy resume --ends-at ...` can override and persist it; otherwise resume
keeps the stored value. `ppy progress --phase done` is refused without a state
change for a review-ending task.

So a completed turn is now judged on evidence. Any of these means the task lands
in `worker_stopped` instead:

| Signal | What it means |
| --- | --- |
| The newest progress note is not the stored terminal phase | The worker never said it got to the agreed handoff. |
| A `done` task's branch holds commits no remote has | The work was never pushed, so nobody but that worktree has it. Review-ending tasks deliberately leave this delivery step to the manager. |
| The session's last tool call was a backgrounded command | Whatever it was waiting on died with the session. |

"Commits no remote has" is asked by patch, not by SHA, so a branch that a stack
cascade rebased upstream reads as pushed rather than as a pile of unpushed work.
The check runs *before* the supervisor's end-of-task auto-commit, so the
supervisor's own safety commit is never what makes a worker look like it withheld
work.

`ppy run` surfaces the reason next to the event, `ppy task <id>` shows it, and
`ppy resume <id>` **with no `--message`** sends the worker back in with what was cut
short — the unfinished gate, the unpushed commits, and the instruction to run the
suite in the foreground, file the done note, and push. Claude workers are told the
same thing up front in the injected command rules.

For repeatable proof, `ppy receipt <task> -- <command>` runs in that task's clean,
resolved process environment, writes combined output to the task evidence directory
as `.txt`, appends the UTC/command/exit/elapsed/head result to `receipts.txt`, and
propagates failures.

### The push the harness finishes for the worker

Unpushed commits are the one signal that does not need a resume. On 2026-09-04
three of four workers filed a done note and still could not get their commits onto
the remote — a push hook failing on advisories already on the default branch, a
push held by the permission layer, a turn that ended first — and the manager
pushed each lease branch by hand. A lease branch is named after its task and
nothing else writes to it, so that push is always safe and the harness makes it.

When a completed turn's *newest* note is `done` and the branch still has commits,
the supervisor pushes the lease worktree's head to that branch itself (no force,
after the auto-commit so the safety commit goes up too), records it as a
`pushed_by_manager` event naming the branch and the SHA, judges the turn again on
what is true afterwards, and only then lands on `worker_done`. A turn that stopped
*before* `done` is missing more than a push, so nothing is pushed and it stays
`worker_stopped` exactly as before.

That post-turn push first proves the task's recorded lease is still active. If a
`lease_released` event returned the slot, the push is skipped and a
`push_skipped_no_live_lease` event records why; the stale task path is never used.

If the remote refuses the push, the task stays `worker_stopped` and the reason
carries the last 20 lines of the push's stderr, so `ppy task <id>` shows the hook's
own words rather than a guess. `ppy task push <id>` is the same push run by hand —
for that task once the hook is fixed, or for any task whose lease worktree is
still on disk. It prints the SHA it pushed, or the refusal it got.

## Steering a live worker

`ppy steer <task> --message ...` picks its mode from the provider's proven
capabilities (the machine-local `.ppy/provider-capabilities.json` if a local
probe wrote one for that provider, otherwise the tracked
`provider-capabilities.json`), and reports which one it used:

| Mode | When | What happens |
| --- | --- | --- |
| `interrupt_resume` | The provider proved resumable mid-work interrupts (Claude) **and** a turn is live | The live runner is retired, the turn is interrupted, and the session is auto-resumed with the steer text. **The task stays `in_progress` throughout.** |
| `checkpoint_pending` | The provider cannot take a mid-flight steer (Codex) and a turn is live | The message is queued and delivered by resuming the session the moment the current turn ends. Queued messages are **additive**: every message since the last delivery goes into that one resume, in the order sent, each labelled — a later message supplements the earlier ones. `ppy steer --replace` queues a message that **supersedes** everything queued before it; superseded messages are named in the `steer_applied` event and never replayed. The response lists the queue and says which messages will be delivered and which superseded, so the CLI never promises delivery of a message that will not survive. |
| `resume` | No turn is live (finished, delivered, blocked, failed, or the process is gone) | The session is resumed immediately with the steer text, because there is no checkpoint coming. |

The interrupt path used to leave the task `failed`. The interrupted process exits
non-zero with no result event, which the adapter correctly reads as a failed
*session* — but a steer is a redirection, not a failure. So the runner is marked
**superseded** before the signal goes out; its exit is recorded as a
`runner_superseded` event against the retired session id and never touches the
task. A manual `ppy resume` is no longer needed after a steer.

## Continuations that admission refuses

A worker ending releases its slot before its automatic continuation — a queued
checkpoint steer, a stored-decision answer, the resume after a steer's interrupt
— and a competing dispatch can take that slot, or the ceiling may have been
lowered since the worker launched. A continuation is **consumed only by the
launch that carries it**: the `resumed` event records the `steer_events` or the
`answered_question` it delivered, and `steer_applied` / `auto_answered` are
written after that launch, never before. A refused resume therefore changes
nothing about what is pending. It writes one `continuation_deferred` event
(actionable; repeated refusals for the same reason add nothing) and leaves the
task in a truthful status: `worker_done` or `worker_stopped` with the steer
still queued, `blocked` with the question still open, and — for an interrupted
worker whose resume was refused — `worker_stopped` rather than an `in_progress`
that no process backs.

Pending continuations are retried, each at most once per pass, whenever a
worker slot frees, at the end of `ppy reconcile`, and on the supervisor's health
tick — which is how they survive a supervisor restart. Every retry goes through
ordinary admission and the ceiling; nothing is bypassed to make a continuation
land. A manual `ppy resume --message` in the meantime supersedes nothing: queued
steers are still delivered at the next checkpoint.

## Superseded sessions

Every resume — an explicit `ppy resume`, an auto-answer, a queued steer landing at
its checkpoint, or the auto-resume after a steer's interrupt — starts a *new*
provider session and retires the old one. Retired runners are recorded with
`superseded_at` and their session id, and at finalization they:

- do **not** set the task's status,
- do **not** raise an actionable `worker_done` / `question` / `error` event,
- record a `runner_superseded` event carrying the session id, exit code, and what
  the retired session's result would have been.

This closes the window where a superseded session's late `exit=1` overwrote
`in_progress` with `failed` while the resumed worker was alive and working. Every
terminal event now also carries the `session_id` it came from, so a late arrival
is attributable rather than anonymous.

## Resume

`ppy resume <task> [--message ...]` flips the task to `in_progress` **before** the
resumed process is started, so a task that was `failed`, `blocked`, or
`needs_recovery` stops reading as stalled the moment work resumes rather than
whenever the new worker happens to emit its first event.

A released lease identity and its old path are never reused. When that worktree is
gone, a resumable task gets a **new lease** on the same branch/base, then resume
proves the new lease owns the task and path before branch synchronization or worker
launch. If the old path still exists but the task's recorded lease is released,
mismatched, or owned by another task, resume refuses and names the
`lease_released` event; that is a recycled slot, not a rebuild candidate.

## Cascade: a worktree whose branch was rewritten upstream

A stack's upper branches are *rebased* when the layer below merges, so a task's
worktree can be sitting on commits that no longer exist on its own branch. Both
`ppy resume` and `ppy deliver` check for that first:

| What git says | What happens |
| --- | --- |
| The remote branch holds commits the worktree lacks, and the worktree holds nothing whose patch is not already upstream | The worktree is reset onto the remote branch and a `worktree_cascaded` event records both commits. |
| Both sides hold commits nothing upstream matches | The command is **refused**, naming both commits. Nothing is force-pushed: which commits survive is a judgement call, not a default. |
| Anything else | Nothing happens. |

"Holds commits the remote lacks" is a question about *patches*, not SHAs
(`git cherry`): a rebase gives every replayed commit a new SHA, so counting SHAs
would read a freshly cascaded branch as full of unpushed work and refuse the whole
stack. A cascade does move the head, so a review approved before it no longer
binds — the delivery gate asks for a re-review at the new commit.
