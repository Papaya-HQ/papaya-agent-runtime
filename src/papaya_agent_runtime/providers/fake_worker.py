"""The fake worker subprocess.

Emits a normalized JSONL event stream on stdout and performs a real commit in its
leased worktree, so the runner/spool/supervisor path is exercised end to end
without any model. Behavior is driven by instruction markers:

- ``ASK:<question>`` -> emit a blocked/question result and stop (no commit).
- ``FAIL`` -> emit no result event and exit non-zero (failure path).
- ``HOLD:<seconds>`` -> stay in a live turn for that long before finishing, so a
  test can steer, interrupt, or supersede a worker that is genuinely mid-turn.
- ``NOCOMMIT`` -> write work AND an evidence directory, then finish without
  committing, so the supervisor's end-of-task auto-commit is what runs.
- ``NODONE`` -> finish without filing a ``--phase done`` progress note.
- ``NOPUSH`` -> finish without pushing the branch.
- ``BACKGROUND`` -> make the session's last tool call a backgrounded command, the
  shape that gets killed when the turn ends.
- otherwise -> write a file, commit, push, file a done note, and emit a completed
  result with the head SHA.

A worker that ends its turn without a done note, without pushing, or waiting on a
backgrounded command is not done — the supervisor says so (see
:mod:`papaya_agent_runtime.turn_end`). The default path here models a worker that
*finished*; the markers above model each way of not finishing.

Whatever the path, this worker never pushes its stub anywhere but a local path:
see :func:`_check_push_target`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


#: Set to ``1`` to let the fake worker push somewhere that is not a local path.
#: Only a deliberate end-to-end test against a real remote should ever set it.
ALLOW_REMOTE_PUSH_ENV = "PPY_FAKE_ALLOW_REMOTE_PUSH"


class RemotePushRefused(RuntimeError):
    """The fake worker was pointed at a remote it must not write to."""


def _check_push_target(worktree: str) -> None:
    """Refuse to push a stub anywhere but a path on this machine.

    This worker's whole output is a placeholder file and a commit that says
    nothing. On 2026-09-04 a dispatch that fell back to the fake provider pushed
    exactly that to github.com within seconds, and the branch had to be deleted by
    hand (issue #49). A remote with a URL scheme or an scp-style ``user@host:``
    prefix is somewhere real, so the push stops before it starts.
    """
    from papaya_agent_runtime.repos import is_local_remote

    url = _git(["remote", "get-url", "origin"], cwd=worktree)
    if is_local_remote(url):
        return
    if os.environ.get(ALLOW_REMOTE_PUSH_ENV) == "1":
        return
    raise RemotePushRefused(
        f"the fake provider refuses to push to {url} — that is not a local path, and this "
        "worker only writes a stub. Dispatch with --provider claude or --provider codex, or "
        f"set {ALLOW_REMOTE_PUSH_ENV}=1 for a deliberate end-to-end test."
    )


def _finish_the_gate(spec: dict, session_id: str) -> None:
    """What a worker that actually finished does: push the branch, file a done note.

    Both are evidence the supervisor checks before calling a turn ``worker_done``,
    so the compliant path has to do them; the ``NOPUSH`` / ``NODONE`` markers take
    each away to model a turn that ended mid-gate.

    Raises :class:`RemotePushRefused` — before pushing anything — when the branch
    would land on a remote that is not a local path.
    """
    instructions = spec.get("instructions", "")
    branch, worktree = spec.get("branch"), spec["worktree_path"]
    if branch and "NOPUSH" not in instructions:
        _check_push_target(worktree)
        _git(["push", "--quiet", "origin", f"HEAD:{branch}"], cwd=worktree)
    if "NODONE" not in instructions:
        from papaya_agent_runtime import progress

        progress.record(
            int(spec["task_id"]), phase="done", note=f"finished {spec['title']}; branch pushed"
        )
    if "BACKGROUND" in instructions:
        # The incident's shape: the last thing the session did was hand a long
        # command to the background, and the turn ended waiting for it.
        _emit(
            {
                "type": "assistant",
                "session_id": session_id,
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "make test", "run_in_background": True},
                    }
                ],
            }
        )


def _hold_seconds(instructions: str) -> float:
    for token in instructions.split():
        if token.startswith("HOLD:"):
            try:
                return max(0.0, float(token[len("HOLD:") :]))
            except ValueError:
                return 0.0
    return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec)

    session_id = spec.get("resume_session_id") or uuid.uuid4().hex
    _emit({"type": "session", "session_id": session_id})
    _emit({"type": "assistant", "session_id": session_id, "text": f"starting {spec['title']}"})
    try:
        return _work(spec, session_id)
    except RemotePushRefused as exc:
        # No result event and a nonzero exit: the adapter reads that as a failure
        # and carries this message, naming the remote, into the worker result.
        _emit({"type": "error", "session_id": session_id, "text": str(exc)})
        return 4


def _work(spec: dict, session_id: str) -> int:
    instructions = spec.get("instructions", "")
    worktree = spec["worktree_path"]

    if "FAIL" in instructions:
        if "DIRTY_FAIL" in instructions:
            # Leave an uncommitted edit behind so the failure is not pristine.
            with open(f"{worktree}/ppy-fake-partial.txt", "a", encoding="utf-8") as fh:
                fh.write("partial work before failure\n")
        _emit({"type": "error", "session_id": session_id, "text": "instructed to fail"})
        return 3

    hold = _hold_seconds(instructions)
    if hold:
        # A genuinely live turn. An interrupt lands here and exits non-zero with no
        # result event — exactly what a real interrupted worker looks like.
        _emit({"type": "progress", "session_id": session_id, "text": f"holding {hold}s"})
        time.sleep(hold)

    if instructions.startswith("ASK:"):
        question = instructions[len("ASK:") :].strip() or "need direction"
        _emit(
            {
                "type": "result",
                "session_id": session_id,
                "status": "blocked",
                "question": question,
                "summary": "worker is blocked on a question",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            }
        )
        return 0

    # Normal path: make a real change and commit it in the leased worktree.
    steer = spec.get("steer_message")
    fname = f"ppy-fake-{spec['task_id']}.txt"
    with open(f"{worktree}/{fname}", "a", encoding="utf-8") as fh:
        fh.write(f"work for task {spec['task_id']}: {spec['title']}\n")
        if steer:
            fh.write(f"steer: {steer}\n")
    if "NOCOMMIT" in instructions:
        # A worker that left review evidence behind and never committed: the
        # supervisor's end-of-task auto-commit decides what belongs on the branch.
        os.makedirs(f"{worktree}/evidence", exist_ok=True)
        with open(f"{worktree}/evidence/after.png", "w", encoding="utf-8") as fh:
            fh.write("pretend screenshot\n")
        _emit({"type": "progress", "session_id": session_id, "text": "left work uncommitted"})
        _finish_the_gate(spec, session_id)
        _emit(
            {
                "type": "result",
                "session_id": session_id,
                "status": "completed",
                "summary": f"implemented {spec['title']}",
                "usage": {"input_tokens": 250, "output_tokens": 40},
            }
        )
        return 0

    _git(["add", "."], cwd=worktree)
    _git(
        [
            "-c",
            "user.name=PPY Fake",
            "-c",
            "user.email=fake@ppy.local",
            "commit",
            "-qm",
            f"task {spec['task_id']}: {spec['title']}",
        ],
        cwd=worktree,
    )
    head = _git(["rev-parse", "HEAD"], cwd=worktree)
    _emit({"type": "progress", "session_id": session_id, "text": "committed change"})
    _finish_the_gate(spec, session_id)
    time.sleep(0.05)
    _emit(
        {
            "type": "result",
            "session_id": session_id,
            "status": "completed",
            "summary": f"implemented {spec['title']}",
            "head_sha": head,
            "usage": {"input_tokens": 250, "output_tokens": 40},
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
