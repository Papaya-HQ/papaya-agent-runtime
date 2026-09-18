# Example brief: show the delivery state on the order page

This is a complete brief in the shape `ppy brief lint` accepts, kept short. The
four outcome sections come first because they are what the worker plans
against, what steering is checked against, and what every continuation packet
carries (see below). The blocks after them are the ones every brief carries —
see the `brief-a-worker` skill for why each exists.

## Goals

- The order page shows a delivery state line ("Packed", "Out for delivery",
  "Delivered at 14:02") for every order that has one; orders without one show
  nothing new. Acceptance: the three fixture orders in `tests/fixtures/orders/`
  render the three lines exactly as the copy file has them.
- The authoritative contract is `docs/contracts/orders.md` §3 (frozen 2026-09-01).
- `make test` green at the delivered head; the collection-only run has no errors.

## Intent

Support agents answer "where is my order" by opening the order page; today they
open the courier tool as well. The outcome is the answer on the page. Reading it
from the existing `delivery_events` table is the suggested route, not the
requirement — if the table cannot give the state, say so under "Flagged, not
done" rather than inventing a second source.

## In scope

- `web/orders/page.py`, `web/orders/templates/order.html`, and their tests.
- A read of `delivery_events` through the existing repository class.
- The copy file's three new strings, added verbatim from `docs/copy/orders.md`.
- Pre-authorised adjacent changes: the fixture index `tests/fixtures/orders/index.json`.

## Out of scope

- No courier-tool integration, polling, or webhook — the table is the source.
- No redesign of the order page and no new components; one line in the existing
  summary card.
- No changes to `delivery_events` or its migrations; if the state cannot be
  derived from it, stop and flag.
- No backfill of historical orders; no speculative caching.

## Starting commit

`git fetch origin main` then `git reset --hard origin/main`; you must see
`4a37e1a9`. The PR opens against `main`.

## Verification

- Authoritative: `make test` (subsumes the page tests and the copy pin).
- Faster subset while iterating: `uv run pytest tests/web/orders -q`.
- `uv run pytest --collect-only -q` has zero errors (a test module is added).

## Evidence contract

- `.ppy-evidence/order-page-delivered.png`, `-packed.png`, `-out.png`: the three
  fixture orders as rendered, 1280 px wide.
- `.ppy-evidence/make-test.txt`: the full `make test` output at the final head.

## Standing gate policy

`make test` is the gate. A flaky failure is re-run once, then reported with both
outputs; never loosened, never skipped, never called unrelated without checking
main's own runs. After two identical failures with a fix between them, stop and
file it under "Flagged, not done" — the third attempt is the user's call.

## Scope-change protocol

If the brief turns out to be wrong — the table lacks a state, the copy file lacks
a string — stop at the checkpoint, and under "Flagged, not done" state the
conflict, its effect on the Goals and on the timeline, and the smallest correct
alternative. Never widen scope silently; never implement something the Out of
scope section excludes; never invent a substitute requirement.

## Plan note

blocking: stop after posting and wait for the manager's reply. Before any code,
`ppy progress <task> --phase plan --note …` (≤ 16 lines) mapping each Goal to the
files that deliver it and naming anything you would need that Out of scope excludes.

## Closeout checklist

- [ ] Three fixture orders render the exact copy strings; orders without a state unchanged.
- [ ] `make test` green at the final head; collection-only run clean.
- [ ] Evidence files named above exist under `.ppy-evidence/` and are named in the done note.
- [ ] Done note has "Outside scope, required to build" and "Flagged, not done", even if empty.

<!-- brief ends -->

---

## What a continuation packet looks like

A resume or steer replaces the worker's instructions for the turn. So that it
stands on its own, the runtime appends the brief's four sections to every
message it delivers (`resumed` events record `scope_preserved: true`). A
deliberate scope change is something the message *says*; it is never something
the packet drops. For `ppy steer <task> --message "Also handle orders whose
latest event is a return; render it as 'Returned'"`, the worker receives:

```text
Also handle orders whose latest event is a return; render it as 'Returned'

--- Standing scope, carried from the brief. It is unchanged unless the message above says otherwise; anything it excludes stays excluded. ---

## Goals
- The order page shows a delivery state line ("Packed", "Out for delivery",
  "Delivered at 14:02") for every order that has one; orders without one show
  nothing new. Acceptance: the three fixture orders in `tests/fixtures/orders/`
  render the three lines exactly as the copy file has them.
- The authoritative contract is `docs/contracts/orders.md` §3 (frozen 2026-09-01).
- `make test` green at the delivered head; the collection-only run has no errors.

## Intent
Support agents answer "where is my order" by opening the order page; today they
open the courier tool as well. The outcome is the answer on the page. Reading it
from the existing `delivery_events` table is the suggested route, not the
requirement — if the table cannot give the state, say so under "Flagged, not
done" rather than inventing a second source.

## In scope
- `web/orders/page.py`, `web/orders/templates/order.html`, and their tests.
- A read of `delivery_events` through the existing repository class.
- The copy file's three new strings, added verbatim from `docs/copy/orders.md`.
- Pre-authorised adjacent changes: the fixture index `tests/fixtures/orders/index.json`.

## Out of scope
- No courier-tool integration, polling, or webhook — the table is the source.
- No redesign of the order page and no new components; one line in the existing
  summary card.
- No changes to `delivery_events` or its migrations; if the state cannot be
  derived from it, stop and flag.
- No backfill of historical orders; no speculative caching.
```

Had that steer been meant to change a boundary — say, allowing a schema change —
the message would have to say so explicitly ("Out of scope is amended: a
migration adding `returned_at` is now in scope"), because the standing block
still says the opposite and the worker is told the message wins only where it
speaks.
