"""Mode parity: everything `ppy serve` does for its workers, an interactive session does too.

The runtime runs two ways: `ppy serve` (headless turns, rounds on a clock, held Papaya
tickets) and an interactive manager session in a harness. On 2026-09-17 the difference
cost real work: hand-dispatched workers crashed, finished or stopped and nobody heard
for 8 to 21 hours, because every supervision decision lived in serve and most of it only
for held tickets. Shane's rule, standing: the runtime must work just as well either way,
so a supervision capability is never serve-only.

This registry is how that rule is kept. Every method of the two classes where serve
decides things (`rounds.Rounds`, `serve.TicketRunner`) and every serve start remedy is
listed here under one capability, with one of four kinds:

- **shared** — the decision lives in a module both modes call (`shared`), serve acts on
  it one way and an interactive session another (`interactive`: the heartbeat, the
  session hooks, a `ppy` command).
- **host** — the Papaya hold protocol itself (reserving, keeping a lease alive, status
  lines, declining): only the process holding the ticket can do it, and an interactive
  session has no hold to keep.
- **scaffold** — the loop, the turn launcher, facts plumbing: no decision of its own.
- **gap** — serve-only today. Allowed only while it is on :data:`KNOWN_GAPS`, which only
  ever shrinks; every serve start and session start records each open gap as a runtime
  deficiency (:func:`record_gaps`), so it is reported until it is healed.

`tests/test_mode_parity.py` fails when serve gains a method or start remedy this
registry does not name, when a shared capability's module is not called from both
sides, or when a gap is added. That is the flag; healing is moving a capability from
``gap`` to ``shared``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SHARED = "shared"
HOST = "host"
SCAFFOLD = "scaffold"
GAP = "gap"
KINDS = (SHARED, HOST, SCAFFOLD, GAP)


@dataclass(frozen=True)
class Capability:
    name: str
    kind: str
    summary: str
    #: `rounds.Rounds` / `serve.TicketRunner` methods and serve start functions it covers.
    serve: tuple[str, ...]
    #: For shared: the module both sides call.
    shared: str = ""
    #: For shared: the modules an interactive session reaches it through.
    interactive: tuple[str, ...] = field(default_factory=tuple)
    #: For gap: what healing it means, in one line.
    heal: str = ""


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "workers_waiting_on_manager",
        SHARED,
        "a worker that finished, stopped, failed, asked or lost its process is taken up",
        serve=(
            "TicketRunner._watch",
            "TicketRunner._nudged",
            "TicketRunner._answer",
            "TicketRunner._review",
            "TicketRunner._wait_on_person",
            "Rounds._look_at",
            "Rounds._question",
            "Rounds._person_wait",
        ),
        shared="papaya_agent_runtime.owed",
        interactive=(
            "papaya_agent_runtime.watch",
            "papaya_agent_runtime.hooks",
            "papaya_agent_runtime.readiness",
        ),
    ),
    Capability(
        "capability_decisions",
        SHARED,
        "a worker's capability request is decided by the manager; only an escalation asks a person",
        serve=("TicketRunner._decide_capability",),
        shared="papaya_agent_runtime.capability_requests",
        # A session sees the manager's undecided requests in readiness, which the
        # session-start hook and `ppy status` print.
        interactive=("papaya_agent_runtime.readiness",),
    ),
    Capability(
        "worker_checkins",
        SHARED,
        "a live worker silent past its budget, planning too long, or not pushing gets a check-in",
        serve=(
            "Rounds._checkins_due",
            "Rounds._plan_reminder",
            "Rounds._saw_push",
            "Rounds._person_has_it",
            "Rounds._checkin_facts",
            "TicketRunner._checkin",
        ),
        shared="papaya_agent_runtime.supervision",
        interactive=("papaya_agent_runtime.watch", "papaya_agent_runtime.hooks"),
    ),
    Capability(
        "gate_followup",
        SHARED,
        "a stopped or done worker is judged by its recorded gate: steered to run, fix or commit",
        serve=(
            "Rounds._stopped_without_gate",
            "TicketRunner._back_to_gate",
            "TicketRunner._stop_regating",
            "TicketRunner._back_to_commit",
        ),
        shared="papaya_agent_runtime.supervision",
        interactive=("papaya_agent_runtime.owed", "papaya_agent_runtime.cli"),
    ),
    Capability(
        "pull_request_repair",
        SHARED,
        "a delivered pull request that is red, conflicted, behind or reviewed is repaired",
        serve=(
            "Rounds._pull_requests",
            "Rounds._follow",
            "Rounds._start_attempts",
            "Rounds._finish_attempts",
            "Rounds._runtime_ci_red",
            "Rounds._forge_states",
        ),
        shared="papaya_agent_runtime.supervision",
        interactive=("papaya_agent_runtime.watch", "papaya_agent_runtime.owed"),
    ),
    Capability(
        "merged_followup",
        SHARED,
        "a merged pull request updates its work item and cleans its worktree; a green one "
        "left unmerged is said or auto-merged",
        serve=("Rounds._merged", "Rounds._clean_if_merged", "Rounds._green"),
        shared="papaya_agent_runtime.supervision",
        interactive=("papaya_agent_runtime.watch",),
    ),
    Capability(
        "work_item_changes",
        SHARED,
        "comments and edits on a work item being worked reach the manager",
        serve=(
            "TicketRunner._comments",
            "TicketRunner._agent_commented_since",
            "TicketRunner._hear",
            "TicketRunner._listen",
            "TicketRunner._take_pending",
            "TicketRunner._mark_read",
        ),
        shared="papaya_agent_runtime.workitems",
        interactive=("papaya_agent_runtime.watch", "papaya_agent_runtime.owed"),
    ),
    Capability(
        "turn_obligations",
        SHARED,
        "a brief left acceptance criteria on the item; a delivery left its report",
        serve=(
            "TicketRunner._check_acceptance_criteria",
            "TicketRunner._acceptance_criteria",
            "TicketRunner._check_reported",
        ),
        shared="papaya_agent_runtime.workitems",
        interactive=(
            "papaya_agent_runtime.owed",
            "papaya_agent_runtime.watch",
        ),
    ),
    Capability(
        "full_suite_before_review",
        SHARED,
        "the supervisor-owned full suite runs once at the worker's head before review",
        serve=("TicketRunner._full_suite_before_review",),
        shared="papaya_agent_runtime.supervision",
        interactive=("papaya_agent_runtime.cli",),
    ),
    Capability(
        "hygiene",
        SHARED,
        "finished worktrees are pruned and loose ends are surfaced",
        serve=("Rounds._hygiene", "Rounds._loose_ends"),
        shared="papaya_agent_runtime.supervision",
        interactive=("papaya_agent_runtime.watch",),
    ),
    Capability(
        "start_remedies",
        SHARED,
        "config, state, dead runners and gate policies are put right, deficiencies announced",
        serve=(
            "serve.self_setup",
            "serve.keep_config_right",
            "serve.keep_state_right",
            "serve.announce_deficiencies",
        ),
        shared="papaya_agent_runtime.supervision",
        interactive=("papaya_agent_runtime.hooks",),
    ),
    Capability(
        "owner_blocker_reports",
        SHARED,
        "blockers only a person can close are re-checked on a clock and told to the owner",
        serve=("blockers.Watch",),
        shared="papaya_agent_runtime.supervision",
        interactive=("papaya_agent_runtime.watch",),
    ),
    Capability(
        "assigned_work_sweep",
        SHARED,
        "work assigned to this agent that nothing picked up is found",
        serve=("sweep.Sweeper",),
        shared="papaya_agent_runtime.supervision",
        interactive=(
            "papaya_agent_runtime.hooks",
            "papaya_agent_runtime.watch",
            "papaya_agent_runtime.cli",
        ),
    ),
    Capability(
        "owed_lane",
        SHARED,
        "a worker waiting on the manager with no live ticket is sent back, given its turn "
        "keyed on the task, or handed to a person",
        serve=("Rounds._owed_lane", "Rounds._ticket_workers", "lanes.TurnRunner"),
        shared="papaya_agent_runtime.lanes",
        interactive=("papaya_agent_runtime.watch", "papaya_agent_runtime.hooks"),
    ),
    Capability(
        "ledger_lane",
        SHARED,
        "a recorded next step that sat is executed, deferred with a reason, or dropped",
        serve=("Rounds._ledger_lane",),
        shared="papaya_agent_runtime.lanes",
        interactive=("papaya_agent_runtime.watch", "papaya_agent_runtime.hooks"),
    ),
    Capability(
        "deficiency_reporting",
        SHARED,
        "what the runtime recorded about itself is opened as issues on the clock",
        serve=("Rounds._deficiency_lane",),
        shared="papaya_agent_runtime.lanes",
        interactive=("papaya_agent_runtime.watch",),
    ),
    Capability(
        "person_outreach",
        SHARED,
        "a decision, capability request or pull request waiting on a person is said to "
        "them where they are (their DM with this agent, the work item, the desktop) and "
        "said again on a clock until it is answered",
        serve=("Rounds._outreach_lane",),
        shared="papaya_agent_runtime.outreach",
        interactive=(
            "papaya_agent_runtime.watch",
            "papaya_agent_runtime.hooks",
            "papaya_agent_runtime.cli",
        ),
    ),
    Capability(
        "provider_usage_limits",
        SHARED,
        "a turn or worker session the provider's usage limit ended is waited out until the "
        "reset and run or resumed again, never counted as a miss or a failure",
        serve=("TicketRunner._wait_out_limit", "TicketRunner._resume_after_limit"),
        shared="papaya_agent_runtime.limits",
        # The heartbeat's owed lane resumes a limit-ended worker and skips its turns while
        # paused; `ppy workers` and `ppy status --team` say the pause.
        interactive=("papaya_agent_runtime.lanes", "papaya_agent_runtime.team"),
    ),
    Capability(
        "ticket_hold_protocol",
        HOST,
        "holding a Papaya ticket: taking, reserving, keeping it alive, status lines, handing back",
        serve=(
            "TicketRunner.__call__",
            "TicketRunner.take",
            "TicketRunner.reclaim",
            "TicketRunner.forget_reclaim",
            "TicketRunner.stalled",
            "TicketRunner._answer_stall",
            "TicketRunner._keep_alive",
            "TicketRunner.keep_status_line",
            "TicketRunner._say_alive",
            "TicketRunner._activity_line",
            "TicketRunner._liveness_interval",
            "TicketRunner._hold",
            "TicketRunner._status",
            "TicketRunner._enter",
            "TicketRunner._say",
            "TicketRunner._hand_back",
            # A hold ending with nothing to build parks the ticket for the sweep, and
            # a pickup counts the pickups before it: both exist only where a Papaya
            # ticket is taken and held, which a session never does.
            "TicketRunner._park",
            "TicketRunner._note_repetition",
            "TicketRunner._stopped",
            "TicketRunner._comment",
            "TicketRunner._setup_comment",
            "TicketRunner._decline",
            "TicketRunner._record",
            "TicketRunner._record_phase",
            "TicketRunner._wait_for_slot",
            "Rounds._reclaim",
            "Rounds._reoffer_missed",
            "Rounds._offer",
            "Rounds._watch_refusals",
            "Rounds._post",
            "Rounds._env_from_connection",
        ),
    ),
    Capability(
        "loop_and_turn_scaffolding",
        SCAFFOLD,
        "the round loop, the headless turn launcher and the facts a turn is given",
        serve=(
            "Rounds.__init__",
            "Rounds.run",
            "Rounds.start",
            "Rounds.close",
            "Rounds.round_once",
            "Rounds._guarded",
            "Rounds._standalone",
            "Rounds._start",
            "Rounds._round",
            "Rounds._running",
            "Rounds._held_ids",
            "Rounds._ticket_of",
            "TicketRunner.__init__",
            "TicketRunner._work",
            "TicketRunner._brief",
            "TicketRunner._dispatched",
            "TicketRunner._deliver",
            "TicketRunner._delivery_blocked",
            "TicketRunner._turn",
            "TicketRunner._launch_turn",
            "TicketRunner._provider",
            "TicketRunner._rerun_later",
            "TicketRunner._missed",
            "TicketRunner._observe_turn",
            "TicketRunner._memory_on_shared_agent",
            "TicketRunner._sleep",
            "TicketRunner._check_stop",
            "TicketRunner._root",
            "TicketRunner._brief_facts",
            "TicketRunner._answer_facts",
            "TicketRunner._review_facts",
            "TicketRunner._evidence",
            "TicketRunner._deficiency",
            "TicketRunner._fingerprint",
            "TicketRunner._opener_kwargs",
            "TicketRunner._own_agent_id",
            "TicketRunner.agent_kind",
        ),
    ),
)

#: The serve-only capabilities still open. The registry started with eleven (2026-09-17);
#: every one was made shared the same day. This set only shrinks and stays empty: a new
#: supervision capability is built shared from the start, never added here.
KNOWN_GAPS: frozenset[str] = frozenset()


def by_serve_name() -> dict[str, Capability]:
    """Each serve method or function, to the capability that covers it."""
    return {name: capability for capability in CAPABILITIES for name in capability.serve}


def gaps() -> list[Capability]:
    return [c for c in CAPABILITIES if c.kind == GAP]


def record_gaps() -> bool:
    """Record the open gaps as one runtime deficiency; whether one was recorded. Never raises.

    Called at every `ppy serve` start and every interactive session start, so the gaps
    travel the same self-report path as any other runtime defect (a deficiency, then one
    issue on the runtime's repository) until changes heal them. The detail is fixed, so
    every start adds evidence (which gaps are still open) to the one entry rather than
    opening another.
    """
    from papaya_agent_runtime import deficiencies

    open_gaps = gaps()
    if not open_gaps:
        return False
    found = deficiencies.record(
        deficiencies.SERVE_ONLY_CAPABILITY,
        "supervision capabilities registered as serve-only in papaya_agent_runtime.parity",
        # Each capability's heal line is in the registry; the evidence is which remain.
        evidence={"where": ", ".join(c.name for c in open_gaps)},
        scope="parity",
    )
    return found is not None
