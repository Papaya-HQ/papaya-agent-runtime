"""GitHub delivery, gated by the exact-HEAD review.

Delivery is the only path that leaves the machine, so it is the most conservative
step: it refuses unless an approved review is bound to the worktree's current
head. Push uses plain git (works against any origin); PR creation uses ``gh``
(preferred: ``gh-axi`` when present) and is skippable for local fixtures.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from papaya_agent_runtime import compose
from papaya_agent_runtime.companions import pr_tool
from papaya_agent_runtime.review import ReviewError, head_sha, is_approved_at_head
from papaya_agent_runtime.state import init_db, store


class DeliveryError(Exception):
    pass


_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}\Z")
#: A pull request's web URL, wherever it sits in a tool's output. Plain ``gh``
#: prints exactly the URL; ``gh-axi`` prints a structured record followed by
#: help lines, so the last line of stdout was "Run `gh-axi pr checks 72 ...` to
#: monitor CI" and that string was persisted as the PR URL of #72 (issue #73).
_PR_URL_RE = re.compile(r"https?://[^\s`'\"<>()]+/(?:pull|pulls|pr|merge_requests)/\d+")


@dataclass
class DeliveryResult:
    task_id: int
    branch: str
    head_sha: str
    pushed: bool
    pr_url: str | None
    note: str
    #: What tearing down the task's compose stack did, when it owned one.
    compose: dict | None = None
    #: Whether a pull request exists for the branch after this delivery — created
    #: now, or found already open. ``True`` with ``pr_url`` None means the PR is
    #: real but its address could not be read back; retrying creation would only
    #: be refused as a duplicate.
    pr_exists: bool = False


def extract_pr_url(text: str | None) -> str | None:
    """The first pull-request URL anywhere in ``text``, or None."""
    if not text:
        return None
    match = _PR_URL_RE.search(text)
    return match.group(0) if match else None


def pr_number(url: str | None) -> int | None:
    """The number at the end of a pull request URL, or None."""
    tail = str(url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def gh_error(proc: subprocess.CompletedProcess) -> str:
    """What `gh` said when it failed, verbatim: stderr, else stdout, else the exit code.

    On PAP-222 (2026-09-17) the phase line read "PR creation failed" and nothing else:
    the reason had been cut to what fit, and a tool that reports on stdout left nothing.
    """
    said = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return said or f"exit status {proc.returncode} with no output"


def _record_pr_failure(task, error: str, *, updating: bool) -> None:
    from papaya_agent_runtime import deficiencies

    deficiencies.record(
        deficiencies.DELIVERY_FAILED,
        "could not update the open pull request" if updating else "could not open a pull request",
        evidence={
            "task_id": int(task["id"]),
            "run_id": task["run_id"],
            "error": error,
        },
    )


def _lookup_pr_url(tool: str, branch: str, forge_slug: str | None, cwd: str) -> str | None:
    """Ask the forge which open PR has this head branch.

    Asked before creating one, so a second delivery updates the pull request that
    is open, and again when creation reported success without a readable URL. Plain
    ``gh`` answers in JSON; ``gh-axi`` answers in its own text, which is searched
    for a URL the same way the creation output was.
    """
    is_plain_gh = Path(tool).name == "gh"
    argv = [tool, "pr", "list", "--head", branch, "--state", "open"]
    if is_plain_gh:
        argv += ["--json", "url", "--jq", ".[0].url"]
    if forge_slug:
        argv += ["--repo", forge_slug]
    proc = _run(argv, cwd=cwd)
    if proc.returncode != 0:
        return None
    return extract_pr_url(proc.stdout)


def _run(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def _git_lines(worktree: str, *args: str) -> list[str]:
    proc = _run(["git", "-C", worktree, *args])
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _resolved_ref(worktree: str, *candidates: str | None) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        lines = _git_lines(worktree, "rev-parse", "--verify", candidate)
        if lines:
            return lines[0]
    return None


def composition_tasks(conn, task, *, remote: str, base: str | None, head: str) -> list[dict]:
    """Open sibling tasks whose own commits appear in this task's proposed PR diff."""
    worktree = task["worktree_path"]
    if not worktree:
        return []
    default_branch = "main"
    if task["repo_id"]:
        repo = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if repo is not None:
            default_branch = repo["default_branch"] or "main"
    base_name = base or default_branch
    base_sha = _resolved_ref(worktree, f"{remote}/{base_name}", base_name, task["base_sha"])
    if not base_sha:
        return []
    proposed = set(_git_lines(worktree, "rev-list", f"{base_sha}..{head}"))
    if not proposed:
        return []
    rows = conn.execute(
        "SELECT * FROM tasks WHERE repo_id = ? AND id != ? "
        "AND (merged_sha IS NULL OR merged_sha = '') "
        "AND status NOT IN ('closed', 'cancelled') ORDER BY id",
        (task["repo_id"], task["id"]),
    ).fetchall()
    conflicts: list[dict] = []
    for sibling in rows:
        sibling_head = _resolved_ref(
            worktree,
            f"{remote}/{sibling['branch']}" if sibling["branch"] else None,
            sibling["branch"],
        )
        if sibling_head is None and sibling["lease_id"] and sibling["worktree_path"]:
            lease = conn.execute(
                "SELECT * FROM leases WHERE id = ? AND status = 'active'",
                (sibling["lease_id"],),
            ).fetchone()
            if (
                lease is not None
                and lease["task_id"] == sibling["id"]
                and lease["worktree_path"] == sibling["worktree_path"]
            ):
                sibling_head = _resolved_ref(sibling["worktree_path"], "HEAD")
        sibling_base = sibling["base_sha"]
        if not sibling_head or not sibling_base:
            continue
        own_commits = set(_git_lines(worktree, "rev-list", f"{sibling_base}..{sibling_head}"))
        shared = sorted(proposed & own_commits)
        if shared:
            conflicts.append(
                {
                    "task_id": int(sibling["id"]),
                    "title": sibling["title"],
                    "commits": shared,
                }
            )
    return conflicts


def _pr_tool() -> str | None:
    # One resolver for both directions: see ``companions.pr_tool``.
    return pr_tool()


def _pr_body(task_id: int, head: str, body_file: str | None) -> str:
    """The pull request body: the caller's file if given, else a composed one.

    ``--body-file`` overrides everything — a manager who has written the body by
    hand should never have it second-guessed. Otherwise the body is composed from
    the brief, the worker's reports, and the review; see
    :mod:`papaya_agent_runtime.pr_body`.
    """
    if body_file:
        return Path(body_file).expanduser().read_text(encoding="utf-8")
    from papaya_agent_runtime import pr_body

    return pr_body.compose(task_id, head_sha=head)


def _forge_for_task(conn, task) -> tuple[str, str | None]:
    """The remote to push through and the ``owner/name`` to open the PR against.

    A repo registered from a local path has a local ``origin``: pushing there
    reaches nobody and there is no forge to open a pull request on. The
    registration now carries the forge URL, and the base clone gets a ``forge``
    remote when its ``origin`` is not it — so delivery pushes and opens the pull
    request where the work is actually reviewed.
    """
    from papaya_agent_runtime import repos

    repo_id = task["repo_id"] if "repo_id" in task.keys() else None  # noqa: SIM118 - sqlite3.Row
    if not repo_id:
        return "origin", None
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return "origin", None
    try:
        return repos.upstream_remote(row), repos.forge_slug(row["forge_url"])
    except repos.RepoError:
        return "origin", repos.forge_slug(row["forge_url"])


def deliver(
    task_id: int,
    *,
    push: bool = True,
    open_pr: bool = True,
    remote: str | None = None,
    base: str | None = None,
    title: str | None = None,
    body_file: str | None = None,
) -> DeliveryResult:
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise DeliveryError(f"task {task_id} not found")
    forge_remote, forge_slug = _forge_for_task(conn, task)
    remote = remote or forge_remote
    worktree = task["worktree_path"]
    branch = task["branch"]
    if not worktree or not branch:
        raise DeliveryError("task has no worktree/branch to deliver")
    explicit_base = base is not None
    if base is None and "stacked_on" in task.keys():  # noqa: SIM118 - sqlite3.Row: `in` scans values
        # A task dispatched with --base opens its PR against that branch unless
        # the caller says otherwise; stacked PRs no longer depend on remembering.
        base = task["stacked_on"] or None

    # A cascade may have rewritten this branch since the worker last touched it.
    # Delivering from a stale worktree force-pushes the cascade away, so move onto
    # the remote first — or refuse, when both sides hold commits.
    from papaya_agent_runtime import stacks

    cascaded = None
    try:
        result = stacks.sync_worktree_with_remote(task_id)
        cascaded = result if result.get("action") == "reset" else None
    except stacks.StackError as exc:
        raise DeliveryError(str(exc)) from exc

    # A layer whose parent has already merged cannot target the parent's branch;
    # one whose parent has not merged is refused against the default branch, and
    # against a parent branch the forge has not seen, unless --base says otherwise.
    base_note = None
    if base:
        try:
            base, base_note = stacks.delivery_base(
                conn, task, base, explicit=explicit_base, remote=remote
            )
        except stacks.StackError as exc:
            raise DeliveryError(str(exc)) from exc

    head = head_sha(worktree)
    compositions = composition_tasks(conn, task, remote=remote, base=base, head=head)
    if compositions:
        carried = ", ".join(f'task {item["task_id"]} "{item["title"]}"' for item in compositions)
        raise DeliveryError(
            f'refusing composition: task {task_id} "{task["title"]}" would carry commits '
            f"from another open task ({carried}). Keep the layer pull requests and merge them "
            f"bottom-up with `ppy stack merge {task_id}`."
        )

    # The gate: never ship work that was not reviewed at its current head.
    approved, reason = is_approved_at_head(task_id)
    if not approved:
        raise DeliveryError(f"refusing to deliver: {reason}")

    pushed = False
    if push:
        proc = _run(["git", "push", remote, f"HEAD:{branch}"], cwd=worktree)
        if proc.returncode != 0:
            raise DeliveryError(f"git push failed: {proc.stderr.strip()}")
        pushed = True

    pr_url: str | None = None
    pr_exists = False
    pr_updated = False
    pr_error: str | None = None
    note = "pushed" if pushed else "no-op"
    if open_pr:
        tool = _pr_tool()
        existing = _lookup_pr_url(tool, branch, forge_slug, worktree) if tool is not None else None
        if tool is None:
            note = "pushed; no gh/gh-axi found, PR not opened"
        elif existing:
            # A second delivery of the same task — the reconcile lane fixing a red
            # pull request — updates the one that is open. Creating another is what
            # `gh` refuses, and on PAP-222 that refusal read as "PR creation failed".
            pr_url, pr_exists = existing, True
            number = pr_number(existing)
            name = f"PR #{number}" if number else existing
            edit = [tool, "pr", "edit", str(number or existing), "--body"]
            edit.append(_pr_body(task_id, head, body_file))
            if forge_slug:
                edit += ["--repo", forge_slug]
            proc = _run(edit, cwd=worktree)
            if proc.returncode == 0:
                pr_updated = True
                note = f"pushed; {name} updated"
            else:
                pr_error = gh_error(proc)
                note = f"pushed; {name} is open but its body could not be refreshed: {pr_error}"
        else:
            argv = [
                tool,
                "pr",
                "create",
                "--head",
                branch,
                "--title",
                title or task["title"],
                "--body",
                _pr_body(task_id, head, body_file),
            ]
            if base:
                argv += ["--base", base]
            if forge_slug:
                # The worktree's `origin` may be a local clone; name the forge
                # explicitly so `gh` opens the pull request in the right place.
                argv += ["--repo", forge_slug]
            proc = _run(argv, cwd=worktree)
            if proc.returncode == 0:
                pr_exists = True
                pr_url = extract_pr_url(proc.stdout) or extract_pr_url(proc.stderr)
                if pr_url is None:
                    pr_url = _lookup_pr_url(tool, branch, forge_slug, worktree)
                note = (
                    "pushed; PR opened"
                    if pr_url
                    else "pushed; PR opened, but its URL could not be read from the tool's "
                    f"output or looked up by branch — find it with `{Path(tool).name} pr list "
                    f"--head {branch}`; do not create another"
                )
            else:
                # `gh` refuses a second PR for the same head and names the one that
                # exists. That is the retry path: the PR is real, so record it.
                pr_url = extract_pr_url(proc.stderr) or extract_pr_url(proc.stdout)
                if pr_url and "already exists" in (proc.stderr + proc.stdout):
                    pr_exists = True
                    note = "pushed; a PR for this branch was already open"
                else:
                    pr_url = None
                    pr_error = gh_error(proc)
                    note = f"pushed; PR creation failed: {pr_error}"
        if pr_error is not None:
            _record_pr_failure(task, pr_error, updating=pr_exists)

    requires_up_to_date = None
    if pr_exists and forge_slug:
        tool = _pr_tool()
        if tool is not None and Path(tool).name == "gh":
            requires_up_to_date = base_requires_up_to_date(
                tool, forge_slug, base or _default_branch(conn, task), cwd=worktree
            )

    store.set_task_status(conn, task_id, "delivered")
    store.append_event(
        conn,
        kind="delivered",
        payload={
            "task_id": task_id,
            "branch": branch,
            "head_sha": head,
            "pr_url": pr_url,
            "pr_exists": pr_exists,
            "pr_updated": pr_updated,
            "pr_error": pr_error,
            "note": note,
            "remote": remote,
            "forge": forge_slug,
            "base": base,
            "base_note": base_note,
            "cascaded": cascaded,
            # Whether the base's ruleset or protection requires a branch to be up to
            # date before it merges: what makes `BEHIND` a reason to steer, not noise.
            "requires_up_to_date": requires_up_to_date,
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    from papaya_agent_runtime import standalone

    standalone.skip_if_local(conn, task_id, standalone.DELIVERED)
    for extra in (
        cascaded["summary"] if cascaded else None,
        base_note,
    ):
        if extra:
            note = f"{note}; {extra}"
    return DeliveryResult(task_id, branch, head, pushed, pr_url, note, pr_exists=pr_exists)


def _default_branch(conn, task) -> str:
    if task["repo_id"]:
        row = conn.execute(
            "SELECT default_branch FROM repos WHERE id = ?", (task["repo_id"],)
        ).fetchone()
        if row is not None and row["default_branch"]:
            return str(row["default_branch"])
    return "main"


def base_requires_up_to_date(tool: str, forge_slug: str, base: str, *, cwd: str) -> bool | None:
    """Does ``base`` require a pull request's branch to be up to date before it merges?

    Read from the base's active rules (a ruleset's required status checks with the
    strict policy) and then from classic branch protection (`strict`). ``None`` when
    neither could be read: unknown is not a requirement.
    """
    import json

    known = False
    proc = _run([tool, "api", f"repos/{forge_slug}/rules/branches/{base}"], cwd=cwd)
    if proc.returncode == 0:
        try:
            rules = json.loads(proc.stdout)
        except ValueError:
            rules = None
        if isinstance(rules, list):
            known = True
            for rule in rules:
                params = rule.get("parameters") if isinstance(rule, dict) else None
                if (
                    isinstance(rule, dict)
                    and rule.get("type") == "required_status_checks"
                    and isinstance(params, dict)
                    and params.get("strict_required_status_checks_policy")
                ):
                    return True
    proc = _run(
        [tool, "api", f"repos/{forge_slug}/branches/{base}/protection/required_status_checks"],
        cwd=cwd,
    )
    if proc.returncode == 0:
        try:
            checks = json.loads(proc.stdout)
        except ValueError:
            checks = None
        if isinstance(checks, dict):
            return bool(checks.get("strict"))
    return False if known else None


#: The event a merge the forge refused leaves on the task, with why.
MERGE_REFUSED = "merge_refused"
#: The refusal that means the base requires an up-to-date branch.
REFUSED_BEHIND = "up_to_date_required"
_BEHIND_WORDS = ("not up to date", "up-to-date", "behind", "update the branch")


@dataclass
class MergeResult:
    merged: bool
    detail: str
    #: Why the forge refused, when it did: :data:`REFUSED_BEHIND` or ``other``.
    refused: str | None = None


def merge_pull_request(task_id: int, pr: str, method: str = "squash") -> MergeResult:
    """Merge a delivered task's pull request with ``gh pr merge``, and record a refusal.

    Only the rounds call this, and only for a repository that opted into
    `auto_merge`. A refusal because the base requires an up-to-date branch is
    recorded as :data:`MERGE_REFUSED` with :data:`REFUSED_BEHIND`, which is what lets
    a later round steer the worker to update the branch.
    """
    tool = _pr_tool()
    if tool is None:
        return MergeResult(False, "no gh found to merge with")
    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(method, "--squash")
    conn = init_db()
    try:
        task = store.get_task(conn, task_id)
        cwd = task["worktree_path"] if task is not None else None
        _remote, slug = _forge_for_task(conn, task) if task is not None else ("origin", None)
        argv = [tool, "pr", "merge", str(pr), flag]
        if slug:
            argv += ["--repo", slug]
        proc = _run(argv, cwd=cwd if cwd and Path(cwd).is_dir() else None)
        if proc.returncode == 0:
            return MergeResult(True, f"merged with {method}")
        said = (proc.stderr or proc.stdout or "").strip()
        refused = REFUSED_BEHIND if any(w in said.lower() for w in _BEHIND_WORDS) else "other"
        if task is not None:
            store.append_event(
                conn,
                kind=MERGE_REFUSED,
                payload={"task_id": task_id, "pr": str(pr), "reason": refused, "said": said[:500]},
                run_id=task["run_id"],
                task_id=task_id,
            )
        return MergeResult(False, said[:200] or "gh pr merge failed", refused=refused)
    finally:
        conn.close()


def record_merged(task_id: int, merged_sha: str, *, note: str | None = None) -> DeliveryResult:
    """Record delivery against a commit that is already merged upstream. Pushes nothing.

    When the manager merges a task's PR on GitHub outside ``ppy deliver``, the task
    stays ``worker_done``: the heartbeat keeps listing it as work awaiting the
    manager, and the worktree is never eligible for reclamation. There is nothing
    left to push and no review gate to satisfy — the commit shipped — so this is
    pure bookkeeping: it names the merge commit and closes the task out.

    A squash or rebase merge produces a *new* commit that the task's worktree has
    never seen, so the SHA is validated by shape only and taken at its word.
    """
    if not _SHA_RE.match((merged_sha or "").strip()):
        raise DeliveryError(
            f"--merged wants the commit SHA the work landed as; {merged_sha!r} is not one "
            "(7-40 hex characters)"
        )
    merged_sha = merged_sha.strip().lower()
    conn = init_db()
    # Serialize the read-and-set so a watch tick racing a hand-run
    # ``ppy deliver --merged`` produces exactly one delivery/teardown event.
    conn.execute("BEGIN IMMEDIATE")
    task = store.get_task(conn, task_id)
    if task is None:
        conn.rollback()
        raise DeliveryError(f"task {task_id} not found")
    branch = task["branch"] or ""
    recorded = (task["merged_sha"] or "").lower()
    if recorded:
        if recorded != merged_sha:
            conn.rollback()
            raise DeliveryError(
                f"task {task_id} is already recorded merged at {recorded}; refusing to "
                f"replace it with {merged_sha}"
            )
        conn.rollback()
        return DeliveryResult(
            task_id=task_id,
            branch=branch,
            head_sha=recorded,
            pushed=False,
            pr_url=None,
            note=f"merge {recorded[:8]} was already recorded; nothing changed",
        )
    # Record the landing on the task, not only in the event stream: a delivered
    # task never leaves that status, so without a merge on the row the heartbeat
    # would re-ask the forge about this branch on every tick, forever.
    store.update_task_fields(
        conn, task_id, merged_sha=merged_sha, merged_at=datetime.now(UTC).isoformat()
    )
    store.set_task_status(conn, task_id, "delivered")
    store.append_event(
        conn,
        kind="delivered",
        payload={
            "task_id": task_id,
            "branch": branch,
            "head_sha": merged_sha,
            "pr_url": None,
            "merged_outside_mm": True,
            "note": note or "recorded against an already-merged commit; nothing was pushed",
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    from papaya_agent_runtime import standalone

    standalone.skip_if_local(conn, task_id, standalone.MERGED)
    # The task is over, so the database it brought up for itself is over too. This
    # is the last moment anything knows the stack belongs to this task.
    torn_down = compose.teardown_for_task(task_id, trigger="deliver --merged", conn=conn)
    return DeliveryResult(
        task_id=task_id,
        branch=branch,
        head_sha=merged_sha,
        pushed=False,
        pr_url=None,
        note=f"recorded as delivered at {merged_sha[:8]} (already merged; nothing pushed)",
        compose=torn_down,
    )


__all__ = [
    "DeliveryError",
    "DeliveryResult",
    "ReviewError",
    "deliver",
    "extract_pr_url",
    "record_merged",
]
