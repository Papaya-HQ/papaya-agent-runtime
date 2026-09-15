---
name: performance-review
description: >-
  Wrap up the manager's own performance review: pull the evidence packet (outcomes,
  rework, incidents, decisions, and every reflection the team filed about their work
  and about the manager), judge what improved and what did not, propose one to three
  measurable experiments, record the review, and align it with the user. Use when the
  user asks to "do / wrap up / close out the performance review", "assess yourself",
  "how did you do", or when `ppy` reports an assessment is due. Mechanics live in
  `ppy assessment` and `ppy reflect`; this skill owns the judgment and the order.
user-invocable: true
---

# performance-review

A review is only worth doing if it changes what happens next. This skill turns the
evidence `ppy` has been collecting — and the team's own words — into a short, honest
review and a plan the user has signed off on. Never a vanity exercise, never a wall
of numbers.

## Procedure

1. **Is one due, or is this a wrap-up?** `./bin/ppy assessment status`.
   - `ready` / `awaiting_user` → an open cycle: complete or align it (steps 3–5).
   - Nothing open and the user asked anyway → `./bin/ppy assessment tick`; if policy
     says none is due (cooldown, too little work), do a **wrap-up** instead: revisit
     the last aligned plan's experiments against the evidence since, and record the
     outcome in `.ppy/memory/improvements.md` (step 6). Say plainly that no new cycle
     was opened and why.
2. **Read the evidence, all of it.** `./bin/ppy assessment show <id>` gives the packet:
   completed runs, task outcomes, rework and steering counts, incidents, usage,
   decisions recorded and reused, cross-repo misses, the previous plan's actions —
   and **`reflections`**: every note a worker filed with `ppy reflect` since the last
   review, both its self-assessment and its assessment of you. Read the reflections
   first. They are the only channel where the team's view of *your* work reaches
   you; treat a pattern across two or more of them as a finding, and quote them
   where they change a conclusion. Separate observation from interpretation.
3. **Judge.** What improved since the last plan (with the baseline)? What
   underperformed, and what is the most likely cause — yours, the brief's, the
   tooling's? Where do the workers' reflections agree with the numbers, and where do
   they contradict them? A contradiction is the interesting part.
4. **Propose one to three experiments.** Each names the observed problem, likely
   cause, the concrete behavior change, a baseline, a target, and the evidence that
   will prove or disprove it next cycle. Prefer small, reversible operating changes.
   Anything touching authority, ceilings, config, framework code, hooks, skills, or
   safety is a *proposal* until the user approves it. Record with
   `./bin/ppy assessment complete <id> --summary ... --action-json '[...]'`.
5. **Align.** Give the user the review in plain terms — what improved, what did not,
   what the team said, the experiments and how each is measured. Describe every
   reference as what it is; no internal labels. Ask only for the decisions that need
   their authority. Record their answer with `./bin/ppy assessment align <id> ...`.
6. **Make it durable.** Append the aligned outcome to `.ppy/memory/improvements.md`
   (the curated history; SQLite stays authoritative). If a reflection named a
   concrete fix to a brief, a rule, or a repo note, make that fix now — a team
   member's learning that never reaches the next brief was wasted.

## Boundaries

- One cycle at a time; never reopen an aligned plan unless its premises changed.
- The team's reflections are evidence, not verdicts: weigh them, quote them, and say
  where you disagree and why — never bury one because it is unflattering.
- The user sees a review, not a status dump: one screen, results first.
