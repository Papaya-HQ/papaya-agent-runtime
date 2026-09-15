---
name: review-surfaces
description: >-
  How Papaya Agent Runtime gives the user a visual review surface (plans, diffs,
  comparisons, connected decisions) and collects their feedback without freezing
  the conversation. Use when you want to show the user something richer than prose
  and get annotations back. Mechanics live in the bundled `lavish-review` script and
  in `ppy`; this skill owns the judgment — when to use a surface and how to run the
  loop.
user-invocable: false
---

# review-surfaces

When a plan, comparison, or set of connected decisions is easier to *see* than to
read, put it on a review surface and hand control back to the user. The cardinal
rule: **never hold your turn open waiting for their feedback.** A review is a
hand-off — you open the surface, step back, and pick up their notes later.

## Pick a surface

- **Rich (preferred when available): `lavish-axi`.** An interactive HTML surface the
  user can annotate. Drive it with the bundled script, never by calling
  `lavish-axi poll` yourself (that long-polls and will freeze your turn):

  ```bash
  scripts/lavish-review new   <file> --title "…"   # scaffold from the shipped default (below)
  scripts/lavish-review open  <file>   # opens the surface + backgrounds the poller
  # ... end your turn; let the user annotate ...
  scripts/lavish-review drain <file>   # read whatever they've left (non-blocking)
  scripts/lavish-review stop  <file>   # when you're done with the surface
  ```

  Author the HTML yourself. Learn `lavish-axi`'s current authoring surface live
  (`lavish-axi --help`, `lavish-axi playbook <id>` — `input` for anything that
  collects decisions) — do not assume flags. Advanced authoring is fine; just never
  foreground the poll.

## Pick the design

Decide in this order and say which one you used when you hand the surface over:

1. **The user named a look or a design system** — use that.
2. **The subject project has a design system** — the product the surface is *about*,
   which may not be the repo you are in: its Tailwind/theme config, CSS tokens,
   component library, brand assets, or existing styled pages. A surface that
   previews or proposes that product's UI is rendered in that product's system.
3. **Otherwise, the shipped default: `lavish-review new`.** It writes one
   self-contained file with Papaya Agent Runtime's Atlassian-style stylesheet
   (`assets/ads.css`: ADS-named tokens, light and dark, lozenges, section
   messages, dynamic tables, stat tiles, cards, native-control forms, disclosures)
   inlined, and a starter layout (`assets/surface.html`: header → key numbers →
   verdict → one card per decision with its own *Queue answer* form → evidence
   table → folded reference). Replace the placeholder content; keep the classes.
   Do **not** fall back to the Tailwind/DaisyUI CDN snippet that `lavish-axi
   design` suggests — the default exists so surfaces from this manager look like
   one product, render offline, and never depend on a CDN.

  Whichever source: no horizontal overflow at any nesting level (tables go inside
  `.ads-table-wrap`), native controls for choices, one queued prompt per decision
  from the form's submit — never from a radio change — and a clear *Send to Agent*
  path at the end.

- **Local fallback (always available): `ppy`.** If `lavish-axi`/Node is missing, use
  the built-in surface: `ppy artifact <run_id> --title ... --sections '[...]'` to
  write it, then `ppy feedback <artifact_id>` to read the sidecar. Same shape:
  produce, hand back, drain later.

## The loop

1. Produce the surface (Lavish HTML, or `ppy artifact`).
2. `lavish-review open <file>` (or just tell the user where the artifact is).
3. **Return to the user in one line** — "It's up; annotate it and tell me when
   you're ready." Then end your turn. Do not block.
4. When they say they've left notes (or on your next natural turn), `drain` /
   `ppy feedback` to collect annotations, act on them, and report the outcome.
5. `stop` the poller when the review is finished.

If the browser says the agent is not listening, the background poller died:
`lavish-review open <file>` again re-arms it. When your harness offers a *tracked*
background job that wakes you on completion, prefer it over the sidecar poller and
arm `lavish-axi poll <file>` through that facility — the user's answer then
interrupts you rather than waiting for your next turn. Use the wrapper's sidecar
poller only when no completion-aware background facility exists. In either case,
hand the turn back immediately; never hold it open waiting for feedback.

## Write for someone who has not read the source

The surface is read by the user, not by you. Every card, row, badge, and option
must say what the thing *is* — "the plan step that adds per-item summaries and
links (second backend PR)", "the section of the plan on continuity" — never a bare
label like "1B", "§6.3", "rev3", "informs 1B/1D". A label may appear once, in
parentheses, after the description. Badges are the worst offender: a lozenge that
reads "gates PR 1B" is noise; "must be settled before the summaries-and-links PR
starts" is information. Read the finished surface as someone who has never opened
the plan; anything they would have to look up gets rewritten.

## Boundaries

- **Plumbing is the script's job, not yours.** Don't hand-roll `lavish-axi poll`,
  `&`, `nohup`, or pidfiles inline — call `lavish-review`.
- **A frozen prompt is never acceptable.** The only thing you legitimately block on
  is the runtime's own `ppy wait`, which returns on the next actionable event.
- Keep the machinery backstage in what you tell the user: "I've put the plan up for
  you to mark up" — not the command you ran.
