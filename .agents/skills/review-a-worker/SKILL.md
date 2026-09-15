---
name: review-a-worker
description: >-
  How to review a worker's finished work so nothing is approved in slices and
  nothing is sent back on a wrong premise: read the full progress log before
  judging the report, review the exact head, run one consolidated rubric before
  approving anything, verify a spec claim before requesting a change, and know
  when a failing gate is a decision for the user rather than another round. Use at
  every `ppy review` and every CI failure on a delivered PR.
user-invocable: false
---

# review-a-worker

The review gate is exact-head and it is the step that pays for itself: on the
Radar milestone every change request fixed a real defect. What went wrong was the
*order* and the *premise* — approving a slice before the whole rubric had run, and
one checkpoint written from memory of the spec. This is the order.

## 1. Read everything the worker filed, first

- `ppy progress <id>` is the report; `ppy task <id>` shows only the latest note.
  Read the whole log before calling anything "missing" — a worker was nearly sent
  back for evidence it had already filed.
- `ppy review show <id>` prints the latest report above the diffstat. The diff you
  review is the one at the exact head the worker named; if the head moved, start
  over.

## 2. One consolidated rubric, then one verdict

One independent review round per delivery, at review-ready, consolidated, with the
reviewer at high reasoning (never xhigh). If a second round is needed, the brief was
missing a matrix row: name it, add it to the brief template, then request the change.
A task past 12M input tokens gets no further independent round without the user's
word. (Assessment cycle 5: 29 of 89 reviews requested changes, each round costing a
worker stop, a resume and a full gate; 36M input tokens per delivered task.)

Run every applicable check before approving *anything*; never approve the search
box today and the design fidelity tomorrow — the worker recaptures all evidence
and re-runs all gates each round.

- **Outcome**: the brief's Goals, item by item — each acceptance criterion met at
  this head, or named as not met. Work that serves a mechanism no Goal asks for is
  a finding even when it is good work.
- **Scope**: inside In scope and outside Out of scope; existing files touched only
  where the brief allowed, each touch listed under "Outside scope, required to
  build". Anything the Out of scope section excludes that was done anyway is a
  finding; an excluded dependency that turned out to be needed belongs under
  "Flagged, not done" with its effect on the outcome, and the decision is yours or
  the user's under the authority rules — not the worker's. This is one pass of the
  same rubric, not a separate cycle.
- **Contract / data**: wire shapes exactly as frozen; no invented fields; no
  client-side recomputation of server-owned values.
- **Spec structure and signature devices**: compare the built thing to the named
  reference row by row (header sentence, coverage line, columns, action placement,
  side-by-side devices, timeline, sources) — with the screenshots open next to the
  mockup's, not from memory.
- **Canonical copy and keys**: the copy file's strings verbatim; the key table;
  internal vocabulary never on screen.
- **Determinism**: time-sensitive tests under `TZ=UTC`; collection-only run when
  test modules were added; generated artifacts regenerated, frozen fixtures intact.
- **Semantics**: the truth table (concurrency, retry, undo) satisfied cell by cell;
  isolation and permission tests present where the brief listed them.
- **Evidence**: every verification command the brief required, each with its own
  result line, at the exact head; screenshots committed where UI changed, with the
  capture directory named. A done note that says "all gates pass" without per-command
  numbers is not evidence — ask for the numbers, or run the commands yourself at head,
  before approving (2026-08-31, task 36).
- **Open the captures.** For anything with a visual surface, view at least one
  after-capture — the state the change most affects — before `ppy review approve`. A
  report saying "screenshots captured" is not evidence; the pixels are. An action-sheet
  row removal was approved off the diff and the gate report while the worker's own
  unopened capture showed the sheet presenting with more than half dead space, because
  the detent fraction still counted the hidden rows (2026-09-01, task 41).

## 3. Verify before you request a change

A wrong change request costs a worker round and your credibility. Before writing
"remove X": open the spec row, the contract line, or the mockup and confirm. If the
worker's choice matches the source, the checkpoint was wrong — say so in the log.

## 4. When a gate fails on CI

- Read the failing assertion, not the job name. Identify owner: worker, brief,
  environment, or pre-existing on main (check main's own runs before accepting
  "unrelated").
- Two identical failures of the same gate with a code fix between them means the
  decision is the user's: present budget-versus-infrastructure with the numbers.
  Do not prescribe a third fix from a local measurement that CI does not reproduce.
- Accept an outside-scope repair only when the worker proves the gate cannot pass
  without it, and say so in the PR body.

## 5. Approve, deliver, and say what shipped

`ppy review approve <id>` at the exact head, then `ppy deliver <id>` — it opens the
pull request against the task's recorded stack parent (dispatched with `--base`),
or against `main` for a bottom layer; pass `--base` only to override. Then tell the
user in plain terms what landed, what was flagged and not done, and what needs them,
naming the stack and the layer rather than each PR as separate work.

After any conflict resolution that overwrote a file with the base's copy and spliced
the branch's change back in, diff `merge-base..branch` for that file — not just the last
commit — and confirm every hunk is present or deliberately superseded, then run the
file's own focused runtime check where one exists. Splicing back only the conflicted
function once dropped the branch's supporting imports and wiring elsewhere in the same
file, putting a runtime ReferenceError on main that a syntax check could not catch
(2026-09-01, PR #543).

When merging is authorised, merge a stack **bottom-up, one layer at a time**: after
each merge confirm the next layer was retargeted to `main` by GitHub's cascading
rebase (`gh pr view <n> --json baseRefName`) before merging it, and if a task in
the stack is resumed or re-delivered after a cascade, fetch and reset its worktree
to the rebased remote branch first — never force-push a stale head over it.
