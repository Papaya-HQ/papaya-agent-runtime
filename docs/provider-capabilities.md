# Provider capability matrix

Fail-closed record of the installed Claude/Codex CLIs' interrupt and resume behavior, produced by the milestone M0 probe harness (`python -m papaya_agent_runtime.probes`). Every capability defaults to `no` and is set `yes` only by a passing live probe.

Adapters (milestone M3) may claim mid-flight interrupt steering only when the matching flag is `yes`. Otherwise they must fall back to checkpoint-at-completion steering plus a fresh-session recovery packet.

How to read interrupt timing: `resume_after_sigint_tool` is the strongest mid-work proof because it interrupts a real long-running tool command (`sleep 20`, observed `in_progress`). `resume_after_sigkill` uses an uncatchable signal, so it proves crash recovery. `resume_after_sigint_model_turn` only proves that a SIGINT delivered during an active (not yet completed) turn is still resumable; the exact interrupt instant is racy and per-provider, so read each scenario's `base exit`/`dur` detail rather than assuming a mid-generation kill.

| Capability | claude 2.1.251 | codex 0.150.1 |
| --- | --- | --- |
| Durable session/thread id emitted in the event stream (`session_id_in_stream`) | yes | yes |
| Token/usage accounting in the event stream (`usage_in_stream`) | yes | yes |
| Resume by id after a clean completion (`resume_after_clean_exit`) | yes | yes |
| Session transcript persists after exit (`session_survives_process_exit`) | yes | yes |
| Resume after SIGINT during a model turn (`resume_after_sigint_model_turn`) | yes | yes |
| Resume after SIGINT during a long tool command (`resume_after_sigint_tool`) | yes | yes |
| Resume after the process group was SIGKILLed (`resume_after_sigkill`) | yes | yes |
| Resume by id from a linked git worktree (`worktree_resume`) | yes | yes |
| Resuming the same id twice both succeed (`duplicate_resume_ok`) | yes | yes |
| Accepts a follow-up into the same running process (`mid_process_steer`) | yes | no |
| Resume requires a prompt (no promptless follow) (`requires_resume_prompt`) | no | yes |
| A tool child survives a process-group SIGINT (`orphan_tool_process`) | no | no |

## claude 2.1.251

- Model: `haiku`
- Probed at: 2026-08-29T07:04:47.734737+00:00
- Probe harness: 0.0.0

| Scenario | Status | Detail |
| --- | --- | --- |
| clean_complete | proved | clean run exited 0 with session id 556cbf4a-cf11-4e88-820c-1aab7930e373 |
| resume_after_clean_exit | proved | resume by id exited 0 with no not-found marker |
| duplicate_resume | proved | second resume of the same id succeeded |
| worktree_resume | proved | resume from a linked worktree succeeded |
| sigint_model_turn | proved | base exit=0 dur=3.7s interrupt_at_line=13; resumed after SIGINT (resume exit 0) |
| sigint_tool | proved | base exit=0 dur=5.3s; interrupted=True; orphan_sleep=False; worktree_dirty=False; resume exit 0 |
| sigkill_survival | proved | base exit=-9 dur=3.1s interrupt_at_line=None; resumed after SIGKILL (resume exit 0) |
| mid_process_steer | proved | streamed two user messages into one process; both answered |

## codex 0.150.1

- Model: `CLI default`
- Probed at: 2026-08-29T07:05:45.340308+00:00
- Probe harness: 0.0.0

| Scenario | Status | Detail |
| --- | --- | --- |
| clean_complete | proved | clean run exited 0 with session id 01a04c55-d1ff-76c1-bfaf-6f249318a76d |
| resume_after_clean_exit | proved | resume by id exited 0 with no not-found marker |
| duplicate_resume | proved | second resume of the same id succeeded |
| worktree_resume | proved | resume from a linked worktree succeeded |
| sigint_model_turn | proved | base exit=1 dur=0.5s interrupt_at_line=1; resumed after SIGINT (resume exit 0) |
| sigint_tool | proved | base exit=1 dur=6.6s; interrupted=True; orphan_sleep=False; worktree_dirty=False; resume exit 0 |
| sigkill_survival | proved | base exit=-9 dur=3.1s interrupt_at_line=None; resumed after SIGKILL (resume exit 0) |
| mid_process_steer | disproved | codex exec is single-shot; stdin is /dev/null and resume starts a new turn |
