"""Stacked pull requests as a first-class thing `ppy` understands.

Stacked pull requests are the documented working model for related changes in one
repository, but the mechanics were entirely manual: the manager had to look up
the previous task's lease branch and pass it as ``--base``, nothing recorded that
the two tasks were one stack, and nothing noticed that GitHub's cascading rebase
had rewritten a branch out from under a worktree. On 2026-09-03, tasks 88-100
were each dispatched from ``main`` and merged serially, costing a rebase and a CI
rerun per merge.

Three things live here:

- **The chain.** A task records the task it is stacked on, so `ppy stack` can walk
  a stack bottom-up without the manager remembering branch names.
- **The view.** Each layer's branch, pull request, base, review state, and — the
  question that decides whether the next merge is safe — whether the pull
  request's base still matches the layer below it, i.e. whether GitHub's cascade
  has run.
- **Cascade awareness.** A cascade *rewrites* the upper branches. Before a resume
  or a delivery, a worktree whose remote branch has moved on without it is
  fast-forwarded onto the remote; a worktree that has commits of its own *and* is
  behind is refused, naming both commits. A force-push over a rebased branch is
  never the answer: it destroys the cascade and whatever else landed with it.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field

from papaya_agent_runtime.state import init_db, store


class StackError(Exception):
    """A stack operation was refused at its current layer."""


# --------------------------------------------------------------------------- #
# Branch state against the remote
# --------------------------------------------------------------------------- #


@dataclass
class BranchState:
    """How a worktree stands against the branch it pushes to.

    ``ahead`` counts commits whose *patch* is not upstream, not commits whose SHA
    is not upstream. A rebase — which is what GitHub's cascade does — gives every
    replayed commit a new SHA, so counting SHAs would report a freshly cascaded
    branch as full of unpushed work and refuse every resume and delivery in the
    stack. ``git cherry`` answers the question that actually matters: is there
    anything here that upstream does not already contain?
    """

    branch: str
    remote: str
    local_sha: str
    remote_sha: str | None
    ahead: int = 0  # commits here whose patch is nowhere on the remote branch
    behind: int = 0  # commits on the remote that this worktree does not have
    replayed: int = 0  # local commits the remote already holds under a new SHA

    @property
    def on_remote(self) -> bool:
        return self.remote_sha is not None

    @property
    def diverged(self) -> bool:
        return self.ahead > 0 and self.behind > 0

    @property
    def rewritten_upstream(self) -> bool:
        """The remote moved and this worktree has nothing of its own: a cascade."""
        return self.behind > 0 and self.ahead == 0


def _git(worktree: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", worktree, *args], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout.strip()


def branch_state(worktree: str, branch: str, remote: str = "origin") -> BranchState:
    """Compare the worktree's head with ``<remote>/<branch>``, fetching first.

    A branch the remote has never seen comes back with ``remote_sha`` unset and no
    counts — there is nothing to be behind or ahead of yet.
    """
    rc, local = _git(worktree, "rev-parse", "HEAD")
    if rc != 0:
        raise StackError(f"{worktree} is not a readable git worktree")
    fetched, _out = _git(worktree, "fetch", "--quiet", remote, branch)
    if fetched != 0:
        return BranchState(branch=branch, remote=remote, local_sha=local, remote_sha=None)
    rc, remote_sha = _git(worktree, "rev-parse", "FETCH_HEAD")
    if rc != 0 or not remote_sha:
        return BranchState(branch=branch, remote=remote, local_sha=local, remote_sha=None)
    rc, behind_out = _git(worktree, "rev-list", "--count", f"{local}..{remote_sha}")
    behind = int(behind_out) if rc == 0 and behind_out.isdigit() else 0
    rc, cherry = _git(worktree, "cherry", remote_sha, local)
    ahead = replayed = 0
    if rc == 0:
        for line in cherry.splitlines():
            if line.startswith("+"):
                ahead += 1
            elif line.startswith("-"):
                replayed += 1
    return BranchState(
        branch=branch,
        remote=remote,
        local_sha=local,
        remote_sha=remote_sha,
        ahead=ahead,
        behind=behind,
        replayed=replayed,
    )


def push_remote(conn, task) -> str:
    """The remote a task's branch belongs on: its repo's forge, else ``origin``.

    A worktree inherits the base clone's ``origin``, which for a repo registered
    from a local path reaches nobody. Every question about where this branch
    stands — and every push of it — goes through the same answer.
    """
    from papaya_agent_runtime import repos

    repo_id = task["repo_id"] if "repo_id" in task.keys() else None  # noqa: SIM118 - sqlite3.Row
    if not repo_id:
        return "origin"
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return "origin"
    try:
        return repos.upstream_remote(row)
    except repos.RepoError:
        return "origin"


def task_branch_state(conn, task) -> BranchState | None:
    """``branch_state`` for a task, resolved through its repo's forge remote."""
    worktree, branch = task["worktree_path"], task["branch"]
    if not worktree or not branch:
        return None
    return branch_state(worktree, branch, push_remote(conn, task))


def unpushed_commits(conn, task) -> int:
    """How many commits exist only in this task's worktree.

    The same question ``ppy worktree prune`` asks before removing a slot, and the
    one that says whether a worker actually pushed its work. It is answered by
    patch, not by SHA (see :class:`BranchState`), so a branch that GitHub's
    cascade rebased upstream reads as pushed — which it is — instead of as a pile
    of unpushed work. A branch the remote has never seen falls back to "commits on
    no remote at all", because there is no branch to compare against yet.
    """
    worktree = task["worktree_path"]
    if not worktree:
        return 0
    try:
        state = task_branch_state(conn, task)
    except StackError:
        return 0
    if state is not None and state.on_remote:
        return state.ahead
    rc, out = _git(worktree, "rev-list", "--count", "HEAD", "--not", "--remotes")
    return int(out) if rc == 0 and out.isdigit() else 0


def sync_worktree_with_remote(task_id: int) -> dict:
    """Bring a task's worktree onto its rebased remote branch, or refuse.

    Called before ``ppy resume`` and ``ppy deliver``. GitHub's cascade rewrites the
    branches above a merged layer, which leaves the worktree pointing at commits
    that no longer exist upstream; resuming or delivering from there either
    force-pushes the cascade away or opens a pull request full of duplicates.

    - Remote moved, worktree has nothing of its own -> reset onto the remote and
      record it as an event.
    - Both sides have commits -> refuse, naming both commits. A force-push here
      would destroy work; which side is right is the manager's call, not ours.
    - Anything else -> no-op.

    Either way, a stacked task whose worktree now builds on a newer head of its
    parent's branch records that head as its ``base_sha`` (see
    :func:`refresh_stacked_base`).
    """
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise StackError(f"task {task_id} not found")
    result = _sync_worktree(conn, task)
    refresh_stacked_base(conn, task)
    return result


def refresh_stacked_base(conn, task) -> str | None:
    """Record the parent head a stacked task is built on as its ``base_sha``.

    A child dispatched onto a parent branch with no commits yet records the
    default branch's SHA: that is all there was to start from. Once the parent
    pushes and the child rebases onto it, that ``base_sha`` still claims every
    parent commit as the child's own; on 2026-09-17 it made delivery of the
    parent refuse itself as a composition with its own child.

    The parent's branch is fetched from the task's push remote; its head is
    recorded when the worktree's HEAD contains it and the recorded base does not
    already. Only moves forward; a parent that has merged is left alone (its
    branch is gone or about to be, and the child now builds on the default
    branch). Returns the new ``base_sha``, or None when nothing changed.
    """
    parent_id = task["stacked_on_task"]
    parent_branch = (task["stacked_on"] or "").strip()
    worktree = task["worktree_path"]
    if not parent_id or not parent_branch or not worktree:
        return None
    parent = store.get_task(conn, int(parent_id))
    if parent is None or parent["merged_sha"]:
        return None
    fetched, _out = _git(worktree, "fetch", "--quiet", push_remote(conn, task), parent_branch)
    if fetched != 0:
        return None
    rc, parent_head = _git(worktree, "rev-parse", "FETCH_HEAD")
    recorded = task["base_sha"] or ""
    if rc != 0 or not parent_head or parent_head == recorded:
        return None
    contains, _out = _git(worktree, "merge-base", "--is-ancestor", parent_head, "HEAD")
    if contains != 0:
        return None
    if recorded:
        behind, _out = _git(worktree, "merge-base", "--is-ancestor", parent_head, recorded)
        if behind == 0:
            return None
    store.update_task_fields(conn, int(task["id"]), base_sha=parent_head)
    store.append_event(
        conn,
        kind="base_refreshed",
        payload={
            "task_id": int(task["id"]),
            "parent_task_id": int(parent_id),
            "parent_branch": parent_branch,
            "from": recorded or None,
            "to": parent_head,
        },
        run_id=task["run_id"],
        task_id=int(task["id"]),
    )
    return parent_head


def _sync_worktree(conn, task) -> dict:
    task_id = int(task["id"])
    try:
        state = task_branch_state(conn, task)
    except StackError:
        # An unreadable worktree is not this check's problem to report: whatever
        # runs next (a push, a diff, a resume) fails on it with a better message.
        return {"task_id": task_id, "action": "none", "note": "no readable worktree to compare"}
    if state is None or not state.on_remote:
        return {"task_id": task_id, "action": "none", "note": "no remote branch to compare with"}
    if state.diverged:
        raise StackError(
            f"task {task_id}: the worktree and {state.remote}/{state.branch} have both moved "
            f"— worktree at {state.local_sha[:8]} with {state.ahead} commit(s) the remote "
            f"lacks (nothing upstream matches those patches), remote at "
            f"{state.remote_sha[:8]} with {state.behind} the worktree lacks. "
            "This is what a cascade plus local work looks like; sort out which commits "
            "survive before resuming or delivering. Nothing was force-pushed."
        )
    if not state.rewritten_upstream:
        return {"task_id": task_id, "action": "none", "note": "worktree matches the remote branch"}

    rc, _out = _git(task["worktree_path"], "reset", "--hard", "--quiet", state.remote_sha or "")
    if rc != 0:
        raise StackError(
            f"task {task_id}: could not move the worktree onto {state.remote}/{state.branch}"
        )
    payload = {
        "task_id": task_id,
        "branch": state.branch,
        "remote": state.remote,
        "from": state.local_sha,
        "to": state.remote_sha,
        "behind": state.behind,
        "replayed": state.replayed,
        "summary": (
            f"{state.remote}/{state.branch} was rewritten upstream (a stack cascade); "
            f"moved the worktree from {state.local_sha[:8]} to {(state.remote_sha or '')[:8]} "
            f"— its {state.replayed} commit(s) are already there under new SHAs and it held "
            "nothing else"
        ),
    }
    store.append_event(
        conn,
        kind="worktree_cascaded",
        payload=payload,
        run_id=task["run_id"],
        task_id=task_id,
    )
    return {"task_id": task_id, "action": "reset", **payload}


# --------------------------------------------------------------------------- #
# The forge view of a layer
# --------------------------------------------------------------------------- #


def _pr_tool() -> str | None:
    from papaya_agent_runtime.companions import companion_bin

    # This module consumes gh's JSON field contract. gh-axi deliberately has a
    # different surface, so using it with --json makes every PR look absent.
    return companion_bin("gh")


def _run_forge(argv: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def pull_request_for(
    branch: str, *, cwd: str | None = None, slug: str | None = None
) -> dict | None:
    """The pull request opened from ``branch``, or None. Tolerates a missing ``gh``.

    Everything a layer view needs from the forge comes from here, so a machine
    without ``gh`` renders the stack from local state instead of failing.
    """
    tool = _pr_tool()
    if tool is None:
        return None
    argv = [
        tool,
        "pr",
        "view",
        branch,
        "--json",
        "number,baseRefName,state,isDraft,title,url,mergedAt,mergeCommit",
    ]
    if slug:
        argv += ["--repo", slug]
    proc = _run_forge(argv, cwd=cwd)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    merge_commit = data.get("mergeCommit")
    merge_oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    data["merged"] = bool(
        str(data.get("state") or "").upper() == "MERGED"
        and isinstance(data.get("mergedAt"), str)
        and data.get("mergedAt")
        and isinstance(merge_oid, str)
    )
    data["merge_commit"] = merge_oid if isinstance(merge_oid, str) else None
    return data


# --------------------------------------------------------------------------- #
# The stack itself
# --------------------------------------------------------------------------- #


@dataclass
class Layer:
    """One task in a stack, with everything needed to decide what to merge next."""

    task_id: int
    title: str
    status: str
    branch: str | None
    parent_task_id: int | None
    parent_branch: str | None
    review: str
    pr_number: int | None = None
    pr_base: str | None = None
    pr_state: str | None = None
    pr_url: str | None = None
    cascade_pending: bool | None = None
    recorded_merged: bool = False
    next_action: str | None = None
    notes: list[str] = field(default_factory=list)


def _review_state(task_id: int) -> str:
    from papaya_agent_runtime.review import ReviewError, is_approved_at_head, latest_review

    review = latest_review(task_id)
    if review is None:
        return "not reviewed"
    try:
        approved, reason = is_approved_at_head(task_id)
    except ReviewError:
        return f"{review['verdict']} (worktree unreadable)"
    return "approved at head" if approved else reason


def _chain_for_task(conn, task_id: int) -> list:
    """The whole stack a task belongs to, bottom-up.

    Walks to the bottom of the chain, then back up through the children — naming
    any layer renders the same stack, because "what has to merge before this" is
    the question, whichever layer the manager happens to be looking at.
    """
    bottom = store.get_task(conn, task_id)
    if bottom is None:
        raise StackError(f"task {task_id} not found")
    climbed = {int(bottom["id"])}
    while True:
        parent_id = bottom["stacked_on_task"] if "stacked_on_task" in bottom.keys() else None  # noqa: SIM118
        if not parent_id or int(parent_id) in climbed:
            break
        parent = store.get_task(conn, int(parent_id))
        if parent is None:
            break
        climbed.add(int(parent["id"]))
        bottom = parent
    chain = [bottom]
    walked = {int(bottom["id"])}
    while True:
        child = conn.execute(
            "SELECT * FROM tasks WHERE stacked_on_task = ? ORDER BY id LIMIT 1",
            (int(chain[-1]["id"]),),
        ).fetchone()
        if child is None or int(child["id"]) in walked:
            break
        walked.add(int(child["id"]))
        chain.append(child)
    return chain


def stack_layers(identifier: int) -> list[Layer]:
    """The stack containing task ``identifier``, bottom-up.

    When no task has that id, ``identifier`` is read as a run id and every stacked
    task in the run is rendered instead — the two ways a manager refers to work in
    flight.
    """
    conn = init_db()
    if store.get_task(conn, identifier) is not None:
        chain = _chain_for_task(conn, identifier)
    elif store.get_run(conn, identifier) is not None:
        chain = list(
            conn.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY id", (identifier,)
            ).fetchall()
        )
        if not chain:
            raise StackError(f"run {identifier} has no tasks")
    else:
        raise StackError(f"no task or run {identifier}")

    from papaya_agent_runtime import repos

    layers: list[Layer] = []
    for index, task in enumerate(chain):
        slug = None
        repo_row = None
        if task["repo_id"]:
            repo_row = conn.execute(
                "SELECT * FROM repos WHERE id = ?", (task["repo_id"],)
            ).fetchone()
            if repo_row is not None:
                slug = repos.forge_slug(repo_row["forge_url"])
        below = chain[index - 1] if index else None
        expected_base = below["branch"] if below is not None else (task["stacked_on"] or None)
        layer = Layer(
            task_id=int(task["id"]),
            title=task["title"],
            status=task["status"],
            branch=task["branch"],
            parent_task_id=(
                int(task["stacked_on_task"]) if task["stacked_on_task"] else None  # type: ignore[index]
            ),
            parent_branch=task["stacked_on"],
            review=_review_state(int(task["id"])),
            recorded_merged=bool(task["merged_at"]),
        )
        pr = (
            pull_request_for(task["branch"], cwd=task["worktree_path"], slug=slug)
            if task["branch"]
            else None
        )
        if pr is None:
            layer.notes.append("no pull request found (or no gh on this machine)")
        else:
            layer.pr_number = pr.get("number")
            layer.pr_base = pr.get("baseRefName")
            layer.pr_state = (
                "merged" if pr.get("mergedAt") or task["merged_at"] else pr.get("state")
            )
            layer.pr_url = pr.get("url")
            if expected_base:
                layer.cascade_pending = layer.pr_base != expected_base
                if layer.cascade_pending:
                    layer.notes.append(
                        f"pull request targets {layer.pr_base!r}, not the layer below "
                        f"({expected_base!r}) — the cascade has already retargeted it, or "
                        "it was opened against the wrong branch"
                    )
        layers.append(layer)
    first_pending = next((layer for layer in layers if not layer.recorded_merged), None)
    for layer in layers:
        if layer.recorded_merged:
            layer.next_action = "merged and recorded"
        elif layer is first_pending:
            layer.next_action = (
                f"deliver task {layer.task_id} first"
                if layer.pr_number is None
                else f"ppy stack merge {layer.task_id}"
            )
        else:
            layer.next_action = f"wait for task {first_pending.task_id} to merge"
    return layers


def render_stack(layers: list[Layer]) -> str:
    """The stack bottom-up, one block per layer, in the order it must be merged."""
    if not layers:
        return "no layers"
    out: list[str] = []
    for index, layer in enumerate(layers, start=1):
        base = layer.parent_branch or "main"
        head = f'{index}. task {layer.task_id} "{layer.title}" [{layer.status}]'
        out.append(head)
        out.append(f"     branch {layer.branch or '(none)'} onto {base}")
        if layer.pr_number:
            out.append(
                f"     PR #{layer.pr_number} ({layer.pr_state or '?'}) -> base "
                f"{layer.pr_base or '?'}"
            )
        for note in layer.notes:
            out.append(f"     note: {note}")
        out.append(f"     review: {layer.review}")
        out.append(f"     next: {layer.next_action}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Native bottom-up merge
# --------------------------------------------------------------------------- #


@dataclass
class StackMergeResult:
    identifier: int
    merged: list[dict] = field(default_factory=list)
    retargeted: list[dict] = field(default_factory=list)
    stopped: str | None = None


def _repo_context(conn, task) -> tuple[str | None, str, str | None]:
    """Forge slug, default branch, and a usable command cwd for one layer."""
    from papaya_agent_runtime import repos

    default_branch = "main"
    slug = None
    cwd = task["worktree_path"]
    if task["repo_id"]:
        repo = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if repo is not None:
            default_branch = repo["default_branch"] or "main"
            slug = repos.forge_slug(repo["forge_url"])
            cwd = cwd or repo["local_path"]
    return slug, default_branch, cwd


def _require_merge_authority() -> None:
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        allowed = load_config().authority.merge
    except ConfigError as exc:
        raise StackError(f"cannot merge without a valid config: {exc}") from exc
    if not allowed:
        raise StackError(
            "merge authority is off; a human must enable it with "
            "`ppy config authority --allow-merge` before running `ppy stack merge`"
        )


def required_checks(pr_number: int, *, cwd: str | None, slug: str | None = None) -> list[dict]:
    """Required check rows for a PR. An unreadable forge is a refusal, not green."""
    tool = _pr_tool()
    if tool is None:
        raise StackError("cannot read required checks because plain `gh` is unavailable")
    argv = [tool, "pr", "checks", str(pr_number), "--required", "--json", "name,bucket"]
    if slug:
        argv += ["--repo", slug]
    proc = _run_forge(argv, cwd=cwd)
    try:
        rows = json.loads(proc.stdout)
    except (TypeError, ValueError) as exc:
        raise StackError(f"could not read required checks for PR #{pr_number}") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise StackError(f"could not read required checks for PR #{pr_number}")
    return rows


def _red_checks(checks: list[dict]) -> list[str]:
    red = []
    for check in checks:
        bucket = check.get("bucket")
        if not isinstance(bucket, str):
            continue
        if bucket.lower() in {"fail", "cancel"}:
            name = check.get("name")
            red.append(name if isinstance(name, str) and name else "a required check")
    return sorted(red)


def _merge_sha(pr: dict | None) -> str | None:
    """A strictly typed merge confirmation from the normalized forge payload."""
    if not isinstance(pr, dict) or pr.get("merged") is not True:
        return None
    value = pr.get("merge_commit")
    if not isinstance(value, str):
        commit = pr.get("mergeCommit")
        value = commit.get("oid") if isinstance(commit, dict) else None
    if not isinstance(value, str):
        return None
    from papaya_agent_runtime.delivery import _SHA_RE

    return value if _SHA_RE.fullmatch(value) else None


def _retarget_children(conn, task, *, new_base: str) -> list[dict]:
    tool = _pr_tool()
    if tool is None:
        raise StackError("cannot retarget child pull requests because plain `gh` is unavailable")
    slug, _default, parent_cwd = _repo_context(conn, task)
    changed = []
    children = conn.execute(
        "SELECT * FROM tasks WHERE stacked_on_task = ? "
        "AND (merged_sha IS NULL OR merged_sha = '') ORDER BY id",
        (task["id"],),
    ).fetchall()
    for child in children:
        if not child["branch"]:
            continue
        cwd = child["worktree_path"] or parent_cwd
        pr = pull_request_for(child["branch"], cwd=cwd, slug=slug)
        if pr is None or pr.get("state") != "OPEN" or pr.get("baseRefName") == new_base:
            continue
        number = pr.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise StackError(f"task {child['id']} has a pull request with no numeric id")
        argv = [tool, "pr", "edit", str(number), "--base", new_base]
        if slug:
            argv += ["--repo", slug]
        proc = _run_forge(argv, cwd=cwd)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "forge refused").strip()[:240]
            raise StackError(f"PR #{number} merged below it, but retarget failed: {detail}")
        changed.append({"task_id": int(child["id"]), "pr": number, "base": new_base})
    return changed


def merge_stack(
    identifier: int,
    *,
    all_layers: bool = False,
    sleep=time.sleep,
    confirm_attempts: int = 10,
) -> StackMergeResult:
    """Merge the lowest unmerged layer natively; optionally repeat upward.

    A layer whose pull request already merged on the forge is *adopted* rather than
    refused: the merge is written down and the stack moves up to the next layer. That
    costs no merge authority, because it merges nothing — without it a stack sat
    behind a shipped pull request with no command able to clear it, since
    ``ppy stack merge`` was both the advice ``stack_layers`` gave and the thing that
    refused (PAP: task 142).
    """
    from papaya_agent_runtime import reconcile

    result = StackMergeResult(identifier)
    authorized = False
    while True:
        layers = stack_layers(identifier)
        layer = next((item for item in layers if not item.recorded_merged), None)
        if layer is None:
            return result
        if layer.pr_number is None:
            result.stopped = f"task {layer.task_id} has no pull request; deliver that layer first"
            return result
        state = str(layer.pr_state or "").upper()
        if state == "MERGED":
            adopted = reconcile.adopt_forge_merge(layer.task_id)
            if not adopted:
                result.stopped = (
                    f"task {layer.task_id} PR #{layer.pr_number} is merged, but the forge did "
                    "not return the commit it landed as; record it with `ppy deliver --merged`"
                )
                return result
            result.merged.append(
                {"task_id": layer.task_id, "pr": layer.pr_number, "merge_commit": adopted}
            )
            if not all_layers:
                return result
            continue
        if state != "OPEN":
            result.stopped = (
                f"task {layer.task_id} PR #{layer.pr_number} is {layer.pr_state or 'unknown'}, "
                "not open"
            )
            return result
        # Only a real merge needs the standing grant, and only once per run.
        if not authorized:
            _require_merge_authority()
            authorized = True
        conn = init_db()
        task = store.get_task(conn, layer.task_id)
        if task is None:
            result.stopped = f"task {layer.task_id} disappeared before merge"
            return result
        slug, default_branch, cwd = _repo_context(conn, task)
        try:
            red = _red_checks(required_checks(layer.pr_number, cwd=cwd, slug=slug))
        except StackError as exc:
            result.stopped = str(exc)
            return result
        if red:
            result.stopped = (
                f"task {layer.task_id} PR #{layer.pr_number} has red required check(s): "
                + ", ".join(red)
            )
            return result
        tool = _pr_tool()
        if tool is None:
            result.stopped = "cannot merge because plain `gh` is unavailable"
            return result
        argv = [tool, "pr", "merge", str(layer.pr_number), "--merge"]
        if slug:
            argv += ["--repo", slug]
        proc = _run_forge(argv, cwd=cwd)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "forge refused").strip()[:240]
            result.stopped = f"forge refused PR #{layer.pr_number}: {detail}"
            return result
        merged_sha = None
        for attempt in range(confirm_attempts):
            confirmed = pull_request_for(layer.branch or "", cwd=cwd, slug=slug)
            merged_sha = _merge_sha(confirmed)
            if merged_sha:
                break
            if attempt + 1 < confirm_attempts:
                sleep(0.5)
        if not merged_sha:
            result.stopped = (
                f"PR #{layer.pr_number} accepted the merge command but the forge did not "
                "return a typed merge commit"
            )
            return result
        from papaya_agent_runtime.delivery import DeliveryError, record_merged

        try:
            record_merged(
                layer.task_id,
                merged_sha,
                note=f"ppy stack merge merged pull request #{layer.pr_number}",
            )
        except DeliveryError as exc:
            result.stopped = str(exc)
            return result
        result.merged.append(
            {"task_id": layer.task_id, "pr": layer.pr_number, "merge_commit": merged_sha}
        )
        try:
            result.retargeted.extend(_retarget_children(conn, task, new_base=default_branch))
        except StackError as exc:
            result.stopped = str(exc)
            return result
        if not all_layers:
            return result


def rebuild_layer(task_id: int) -> dict:
    """Apply the same cascade-safe branch synchronization used before resume."""
    return sync_worktree_with_remote(task_id)


# --------------------------------------------------------------------------- #
# Delivery base
# --------------------------------------------------------------------------- #


def merge_order(conn, task) -> str | None:
    """Where a layer sits in its stack and what must merge before it, from local state.

    Read into `ppy review show` and the pull request body. Only a task that records
    a stack parent gets one; the bottom of a stack, or a task on no stack, is
    described by the pull request body's own default-branch sentence. No forge is
    consulted: the order is a fact about the chain, and the merged marks come
    from what `ppy deliver --merged` recorded.
    """
    parent_id = task["stacked_on_task"] if "stacked_on_task" in task.keys() else None  # noqa: SIM118
    if not parent_id:
        return None
    chain = _chain_for_task(conn, int(task["id"]))
    position = next(
        (i for i, row in enumerate(chain, start=1) if int(row["id"]) == int(task["id"])), None
    )
    if position is None:
        return None
    ahead = chain[: position - 1]
    if not ahead:
        return None
    steps = []
    for row in ahead:
        merged = "merged" if row["merged_at"] else "unmerged"
        steps.append(f'task {int(row["id"])} "{row["title"]}" (branch {row["branch"]}, {merged})')
    parent = ahead[-1]
    return (
        f"stack: layer {position} of {len(chain)}; its pull request targets "
        f"{parent['branch']}. Merge order: "
        + ", then ".join(steps)
        + f", then this task {int(task['id'])}."
    )


def delivery_base(
    conn, task, base: str | None, *, explicit: bool = False, remote: str = "origin"
) -> tuple[str | None, str | None]:
    """The base a stacked layer's pull request should target, and why if it changed.

    When the layer below has already merged, its branch is gone (or about to be):
    opening a pull request against it either fails or shows a diff nobody can
    review. GitHub's cascade retargets an *existing* pull request in that
    situation; a pull request being opened now has to be aimed at the default
    branch itself.

    While the layer below is *not* merged, this layer is refused two ways unless
    ``explicit`` says the caller passed ``--base`` and means it: opening against
    the default branch (the diff would carry the parent's commits and the merge
    would land them twice), and opening against a parent branch the forge has
    never seen (the pull request cannot be created; deliver the parent first).

    Only a task that records a stack parent is checked: a bare ``--base`` says
    where to open the pull request and nothing about a chain, and asking the forge
    about it on every delivery would be a network call for no information.
    """
    parent_id = task["stacked_on_task"] if "stacked_on_task" in task.keys() else None  # noqa: SIM118
    if not base or not parent_id:
        return base, None
    slug = None
    default_branch = "main"
    if task["repo_id"]:
        from papaya_agent_runtime import repos

        repo_row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if repo_row is not None:
            slug = repos.forge_slug(repo_row["forge_url"])
            default_branch = repo_row["default_branch"] or "main"
    parent = store.get_task(conn, int(parent_id))
    parent_short = f"task {int(parent_id)}"
    parent_label = (
        f'{parent_short} "{parent["title"]}" (branch {parent["branch"]})'
        if parent is not None
        else parent_short
    )
    parent_merged = parent is not None and bool(parent["merged_at"])
    if base == default_branch:
        if explicit or parent_merged:
            return base, None
        raise StackError(
            f"refusing to open task {int(task['id'])} against {default_branch}: its stack "
            f"parent {parent_label} has not merged, so this pull request would carry the "
            f"parent's commits too. Merge {parent_short} first, or pass --base "
            f"{default_branch} to open it there anyway."
        )
    pr = pull_request_for(base, cwd=task["worktree_path"], slug=slug)
    if pr is None:
        if explicit or parent_merged or _branch_on_remote(task["worktree_path"], remote, base):
            return base, None
        raise StackError(
            f"refusing to open task {int(task['id'])} against {base}: its stack parent "
            f"{parent_label} has not been pushed to the forge yet, so no pull request can "
            f"target that branch. Deliver {parent_short} first (or `ppy task push "
            f"{int(parent_id)}`), or pass --base to override."
        )
    if not pr.get("mergedAt"):
        return base, None
    return default_branch, (
        f"the layer below (task {int(parent_id)}, pull request #{pr.get('number')} on {base}) "
        f"has merged, so this pull request targets {default_branch} instead of a merged "
        "branch; GitHub's cascade has not retargeted it because it did not exist yet"
    )


def _branch_on_remote(worktree: str | None, remote: str, branch: str) -> bool:
    """Does the forge have this branch at all? Asked only when it has no pull request."""
    if not worktree:
        return False
    rc, out = _git(worktree, "ls-remote", "--heads", remote, branch)
    return rc == 0 and bool(out.strip())
