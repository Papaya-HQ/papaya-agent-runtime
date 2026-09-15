---
name: reflect
description: >-
  A team member's end-of-task reflection: its own self-assessment (what it learned,
  what went well or badly, what would have made the task easier) and its assessment
  of the manager (was the brief clear and complete, was the scope right, did steering
  and review help or hurt). Filed with `ppy reflect`, durable in state, and read into
  the manager's next performance review as first-class evidence. Use at the end of
  every dispatched task, before the final progress report, or whenever the user asks
  a worker to "reflect", "self-assess", or "review the manager".
user-invocable: true
---

# reflect

Every task ends with two short, candid notes from the team member who did it. They
are not a courtesy: the manager's own performance review is built from the evidence
packet, and these notes are the only part of it written by the people the manager
directs. A vague or flattering reflection is worthless; a specific one changes the
next brief.

## When

Right before the final `ppy progress <task_id> --phase done` — after verification,
while the task is still fresh. Also after giving up on a task (`--phase blocked`),
which is when the reflection matters most.

## What to write

Answer in plain sentences; two to five per note; concrete over general. Name files,
commands, and moments, not feelings.

**`--self` — your own assessment**
- What did you learn that the next person should know? (Put durable facts in the
  repo's `notes.md` too; the reflection is for the *lesson*, the note is for the
  *fact*.)
- What went well, and what did you do that caused it?
- What went badly or cost time, and what would you do differently?
- What would have made this task easier — a tool, a fixture, a fact in the brief?

**`--manager` — your assessment of the manager**
- Was the brief clear, complete, and correct? Where did you have to guess, or find
  that it was wrong?
- Was the scope right — anything you were told to build that the plan did not need,
  or told not to touch that you had to?
- Did steering or review help or hurt? Was it early enough to act on? Did a review
  finding surprise you, and should the brief have prevented it?
- What should the manager change for the next task?

## How

    ppy reflect <task_id> --self "<your assessment>" --manager "<your assessment of the manager>"

Both notes together is the norm; one alone is allowed. Up to 4000 characters each.
`ppy reflect <task_id>` shows what has been filed. Never put secrets, credentials, or
another person's private details in a reflection.

## What happens to it

The manager sees it in `ppy task` / `ppy reflect <task_id>` and, more importantly, the
next performance review's evidence packet carries every reflection since the last
review. The `performance-review` skill requires the manager to read them first, treat
a pattern across reflections as a finding, quote them where they change a conclusion,
and fix any concrete brief or rule they name.
