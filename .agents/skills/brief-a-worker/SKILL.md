---
name: brief-a-worker
description: >-
  The checklist a task brief must satisfy before `ppy dispatch` — the things a
  worker cannot recover once it has started: the Goals, Intent, In scope and Out of
  scope sections that make the brief outcome-led and bounded, the exact starting
  commit, the design reference for UI work, one authoritative verification suite,
  the test-hygiene checks that only fail on CI, the evidence contract, gate policy
  and scope-change protocol every brief carries, and the tables that turn prose
  semantics into something a worker can be right about. Use every time you write or
  revise a brief; it exists because each item on it cost a worker cycle between
  2026-08-30 and 2026-09-03.
user-invocable: false
---

# brief-a-worker

A worker does exactly what the brief says and nothing it cannot know. Codex
workers cannot be redirected mid-turn, so a brief that is wrong at the start is a
wasted dispatch. Before `ppy dispatch --brief <file>`, walk this list; every item was
paid for once already.

## 0. Outcome first: Goals, Intent, In scope, Out of scope

Every brief opens with these four sections, under those headings, before any
mechanism. `ppy brief lint` and `ppy dispatch --brief` refuse an empty or missing one
under `--strict` and warn otherwise. They exist because a brief can specify steps
and tests without saying what success is, why it matters, or where to stop — and a
worker then optimises the mechanism, adds an adjacent capability, or hardens
something speculative while the product outcome sits unfinished (issue #77).

- **Goals.** The observable outcomes this task must deliver, its contribution to the
  overall project goal, and the acceptance criteria as pass/fail items. Name the
  authoritative project contract or plan when one exists, with its section. The
  acceptance criteria live here, once; the closeout checklist at the end refers to
  them rather than restating them.
- **Intent.** Why the task is needed, who benefits, and the problem or experience it
  improves. Separate the *outcome* from any *suggested approach*: "read it from
  `delivery_events`" is a route, "the answer is on the page" is the requirement. A
  worker that knows the difference flags a dead route instead of inventing a second
  source.
- **In scope.** The concrete capabilities, files or components, changes and
  verification this task covers — what the worker is authorised to do.
- **Out of scope.** Explicit exclusions and stopping boundaries, including the
  tempting adjacent work: legacy compatibility, redesigns, speculative abstractions,
  unrelated cleanup, additional hardening not needed for the agreed outcome. Name
  them; "nothing else" is not a boundary a worker can check against.

These four travel. The runtime appends them verbatim to every resume and steer
packet as a *standing scope* block, so a continuation stands on its own and cannot
silently drop an exclusion. A deliberate scope change is therefore something a
steer *says* ("Out of scope is amended: …"); it is never something a packet omits.
See [`docs/brief-example.md`](../../../docs/brief-example.md) for a complete short
brief and the packet the worker receives after a steer.

## 1. The packet itself

- **Durable file, never a shell substitution.** Write the brief under
  `.ppy/briefs/<repo>/` and dispatch with `--brief <file>`. The tool refuses an empty
  file; a session restart once wiped a scratch brief and a worker started blank.
- **Exact starting commit, in commands.** Related changes in one repository are a
  **stack** by default (see the runtime contract, "Stacked pull requests"): the
  starting commit for the next layer is the previous task's lease branch, dispatched
  with `ppy dispatch --base <previous lease branch>` so the stack parent is recorded and
  `ppy deliver` targets it. The brief still spells it out: `git fetch origin <branch>`
  then `git reset --hard origin/<branch>` and the SHA the worker must see. Never
  "merge the base in" — a fresh worktree loses nothing and a merge fails when main
  has moved. Start from `origin/main` only for a bottom layer or an independent change.
  Name the base branch the PR opens against (Claude briefs: the runtime's command
  rules already pin the push to the lease branch; name only the base).
- **Scope fence, both directions.** What is in (the plan section, verbatim), what is
  explicitly out (the neighbouring sections by name), and the two report headings
  the worker must fill: "Outside scope, required to build" and "Flagged, not done".
- **The plan-note gate.** Require a `--phase plan` note (≤ 16 lines) before code that
  maps each Goal to the files that deliver it and names anything the plan would need
  that Out of scope excludes — and say you will read it against the four sections.
  A plan that serves a mechanism no Goal asks for, or that quietly reaches into an
  exclusion, is steered before implementation, not reviewed after it.

## 2. UI work: name the design reference

- **Point the worker at the ux-craft skill by absolute path.** Workers are not
  skill-aware — naming a skill is invisible to them, the file read is the mechanism.
  Every brief that touches UX, design, or interactions carries the instruction to read
  and apply it before writing any UI: `<repo>/.agents/skills/ux-craft/SKILL.md` when the
  target repo carries its own copy, otherwise the manager's harness copy
  (`~/.claude/skills/ux-craft/SKILL.md`) — resolved to a real, existing absolute path
  before it goes in the brief. Two iOS chat briefs went out without it and one had to be
  steered mid-flight (2026-08-31).
- **Ask "which mockup are we designing from?" before dispatching.** Pin the file
  (copy it under `.ppy/briefs/<repo>/` if it lives outside the repo) and its written
  spec; say which wins on structure, which on data (the frozen contract), which on
  materials (the repo's design system). A worker sent with tokens but no mockup
  rebuilt eight signature devices as generic UI.
- **Turn the mockup into a row-by-row acceptance checklist**: each signature device
  (header sentence, coverage line, column set, action placement, side-by-side
  quotes, timeline shape, sources pill) named as a pass/fail item with the spec row.
- **Canonical strings and keys are acceptance criteria**, not context: the copy
  file's exact strings, the keyboard table, the "never ship internal vocabulary" rule.
- **Determinism**: times rendered from fixtures use a fixture-pinned IANA zone; the
  unit suite must pass under `TZ=UTC` and under the machine zone before done.
- **Evidence**: committed screenshots for every reference state, sized to compare
  with the mockup's, under the repo's generated-evidence directory.
- **Capture paths are an acceptance item, not a reporting nicety.** "The final report
  MUST name the capture directory; a delivery without it is incomplete" belongs in the
  acceptance/gates block, never in the report block — workers treat gates as binding and
  reporting as prose. Two workers in one day claimed captures without publishing paths:
  a resume round-trip each (2026-09-01, tasks 49 and 52).

## 3. Verification: one authoritative suite, then subsets

- **Name one complete verification command or suite** that subsumes feature tests,
  migration inventory pins, cross-phase compatibility, generated-artifact
  freshness, and performance gates; list focused commands only as faster subsets.
  Two of three CI failures on one PR were pins outside the "focused" list.
- **Collection-only run** (`pytest --collect-only -q`, zero errors) whenever a PR adds
  or renames test modules — parallel test trees without packages refuse duplicate
  basenames, and targeted runs never see it.
- **Fresh-chain proof for migrations**: the database *reset* target, not the
  ensure target, and the expected single head.
- **Generated artifacts**: which commands regenerate them and that they are
  committed; the frozen fixtures that must stay byte-identical.
- **Receipts come from the worker's shell, never from a test.** A test that writes to the
  evidence directory passes locally and errors on CI, where the path does not exist
  (task 91, 2026-09-03). Tests write only under pytest's `tmp_path`; the worker copies
  the dump into the evidence directory with its own commands.
- **Never name `/private/tmp` as the evidence directory.** The worker's shell cannot reach
  it (21 tasks between 2026-09-01 and 09-07 lost a cycle to that). The runtime's
  environment block pins `<worktree>/.ppy-evidence/` — inside the worktree, excluded from
  version control, listed by `ppy review show` — and `ppy repo set --evidence-dir` changes
  it per repo. The brief names the artefacts; the block names the directory.
- **Environment facts come from `ppy repo set`, not from the brief.** The private database
  stack (`compose_project=task_<n>`, its port, the `make`/`.env` override recipe), the
  local gate versus CI's full suite, and the push-hook policy are rendered into every
  dispatched brief from repo config. A brief restating them by hand drifts; a brief that
  needs one changed changes the repo setting instead.
- **Claude-provider briefs do not name a branch.** The runtime's command rules pin the push
  to the task's lease branch (`ppy/task-<n>-...`); a brief naming another branch is ignored
  and confuses the worker. The manager opens the PR from the lease branch.
- **Merge main before pushing a PR that sat**: the forge runs no pull-request checks
  on a conflicting PR, so it shows "no checks" rather than failing.

## 4. The blocks every brief carries

Three named sections, the same names every time, so a worker can find the rule instead
of re-reading the brief for it.

- **Evidence contract.** Which artefacts count as proof and how each is named or sized —
  one line per artefact. The directory is the environment block's `.ppy-evidence/` inside
  the worktree (the worker creates it); do not name another. "Capture screenshots" with no destination produced two receipt-less
  deliveries in one window (2026-09-01).
- **Standing gate policy.** Which single gate is authoritative; what a flaky gate means
  (re-run once, then report the flake with both outputs — never loosen it, never skip
  it, never call it unrelated without checking the base branch's own runs); and when a
  failing gate stops being the worker's problem — after two identical failures with a
  fix in between it is the user's budget-versus-infrastructure decision, so the worker
  files it under "Flagged, not done" instead of trying a third fix. Three workers stalled
  on a gate nobody owned and two gate cycles were lost for want of this block
  (window ending 2026-09-01).
- **Scope-change protocol.** What the worker does when the brief turns out to be wrong
  or an out-of-scope dependency turns up: stop at the checkpoint, and under "Flagged,
  not done" state the conflict, its effect on the Goals and on the timeline, and the
  smallest correct alternative — then wait. Never widen scope silently, never implement
  something Out of scope excludes, never invent a substitute requirement. The manager
  decides under its existing authority rules (routine: decide; scope expansion or a
  product call: the user). A worker resolving a wrong brief on its own cost one
  stale-intent incident and one two-round change request.

- **Adversarial acceptance matrix.** The rows the independent reviewer would otherwise
  find after the worker's first review-ready commit, written into the brief as blocking
  plan-note items with a proof each: every interleaving of two callers on the same row or
  lock (lock order as a partial order); the semantic role of every injected clock
  (eligibility cutoff, retry clock, post-lock admission); JSON scalar equality where
  Python values validate JSON (true is not 1); limit-plus-one and influence-set rows
  (every row hashed into a predicate is an observation); deferred completion after an
  async boundary in both orders; the repository's harness ceilings checked before the
  long gate; an invariant on a released or expired identity (lease, token, slot) that
  never removes the path that mints a fresh one; and one probe of the real tool's
  output shape behind every capacity or state read, never only a fake. Twenty-nine of 89 reviews requested changes in the window ending
  2026-09-11 and 14 workers said the probes belonged in the brief: each late round cost
  a stop, a resume and a full gate. A second review round means a row was missing —
  name it and add it to `.ppy/briefs/_templates/defect-brief.md` the same day.
- **Continuations are deltas.** A steer or continuation names what changed since the
  last packet and nothing else; a full packet is re-issued only when the scope is
  replaced, says so in its first line, and drops superseded operational instructions.
  Seven reflections (tasks 175, 176, 184, 196) lost time reconciling serial
  superseding packets.

Then **one closeout checklist at the end of the brief** — every acceptance item, gate and
receipt in a single list — instead of the same constraints repeated inside each section.
Three separate workers asked for exactly this (2026-08-31); duplicated constraints drift
against each other and the worker has to guess which copy is current.

## 5. Semantics the worker must be right about

- **Concurrency / back-off / retry: a state-transition truth table.** Rows are
  states, columns are events, cells are the next state and side effect — especially
  "own streak" versus "any peer's success". Prose here produced a back-off that
  reset on anyone's success.
- **Contract or freeze documents: an expected-discrepancy matrix.** Plan claim →
  shipped reality → what to do (document, flag, never invent). A worker rediscovered
  a "band" list parameter that never shipped.
- **Pinned predicate / evidence sets come from the gate, not from the prose.** When a
  brief fixes the exact set a thing must carry, derive it by reading the gate that has to
  accept it. A five-predicate set copied out of plan prose could not pass the
  (deliberately unchanged) materiality gate; the worker had to add the missing predicate
  itself (2026-08-31, task 36).
- **Environment facts that bite**: per-worktree databases, package-cache
  sandboxes, CI runner slowdown factors and the budget margins they imply, tools
  that must not run locally.

## 6. Performance briefs

- **Never prescribe a fix from a local measurement when local and CI diverge
  sharply.** State the required outcome (the budget, where it is frozen) and ask for
  CI-side stage timing or query plans first; separate the hypothesis from the
  requirement. A round-trip fix moved a CI number by 3 ms.
- **Escalation is built in**: after two identical gate failures with a code fix in
  between, the budget-versus-infrastructure decision goes to the user with the
  evidence — not a third code round.

## 7. Don't restate the environment's command rules

A `claude` dispatch already prepends them: one plain command per call, no pipes /
`&&` / `;` / inline env assignment / redirection, `cd` on its own, push with
`git push origin HEAD:<branch>`, never open a PR (the manager does that from the
pushed branch), and record any denied command verbatim under "Flagged, not done".
That block is generated from the branch the task actually leased, so it is always
right and always identical. Repeating it by hand in a brief is how it drifts.
Codex workers have a real shell and are deliberately not given the block.

## 8. Before you send it

`test -s` the file; `ppy brief lint` it (the four outcome sections, and the defect
shape when it is one); confirm disk and pool capacity (`ppy dispatch` checks the
disk); read the brief once as the worker: is there any instruction it would have to
guess, and could it tell from the brief alone when it is done and when to stop?
