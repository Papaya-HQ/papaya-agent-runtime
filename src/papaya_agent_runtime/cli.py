"""The ``ppy`` control-plane CLI.

Deterministic policy enforcement lives here and in the modules it calls; the
manager agent never enforces authority or ceilings itself. Subcommands are added
as milestones land. M1 provides: setup, config, doctor, repo, status, version.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from papaya_agent_runtime import __version__
from papaya_agent_runtime.lifecycle import TASK_STATUSES


def _cmd_version(args: argparse.Namespace) -> int:
    print(f"papaya-agent-runtime {__version__}")
    return 0


def _cmd_capabilities(args: argparse.Namespace) -> int:
    """What this runtime is, from local state only — the client's connect-time probe.

    One JSON object on one line, or the same fields one per line for a person. It
    reads no network and no database, so it answers on an unconfigured machine as
    fast as on a working one, and it has no failure mode to report: an absent
    client is `null`, and the exit code is 0 either way.

    The printing itself lives in `capabilities.main`, because `bin/ppy` answers
    this command from the standard library when the project environment has not
    been built yet and the two roads must not print different documents.
    """
    from papaya_agent_runtime import capabilities

    return capabilities.main(["--json"] if args.json else [])


def _cmd_doctor(args: argparse.Namespace) -> int:
    from papaya_agent_runtime import standalone
    from papaya_agent_runtime.setup.doctor import run_doctor

    print(run_doctor(as_json=args.json))
    # Stdout carries the JSON document, so the line goes beside it, not into it.
    standalone.say_invitation(sys.stderr if args.json else sys.stdout)
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.config import ConfigError
    from papaya_agent_runtime.setup.wizard import run_setup

    overrides = _collect_overrides(args)
    try:
        cfg = run_setup(non_interactive=args.non_interactive, overrides=overrides)
    except ConfigError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"configured manager {cfg.manager.provider}/{cfg.manager.model}"
        f"@{cfg.manager.reasoning}; worker ceiling {cfg.worker.provider}/"
        f"{cfg.worker.max_model}@{cfg.worker.max_reasoning}, default "
        f"{cfg.worker.default_model}@{cfg.worker.default_reasoning}, "
        f"concurrency {cfg.worker.max_concurrent}; posture "
        f"{cfg.cost_posture}"
    )
    if not args.skip_tools:
        print("provisioning companion tools (treehouse, lavish-axi, gh-axi)…")
        _print_provision(_provision(force=False))
    return 0


def _provision(*, force: bool, names: list[str] | None = None) -> list:
    from papaya_agent_runtime.paths import ensure_layout
    from papaya_agent_runtime.setup.provision import provision_all

    ensure_layout()
    return provision_all(names=names, force=force)


def _print_provision(results: list) -> None:
    glyph = {"installed": "+", "present": "=", "skipped": "-", "failed": "x"}
    for r in results:
        ver = f" {r.version}" if r.version else ""
        detail = f" — {r.detail}" if r.detail else ""
        print(f"  [{glyph.get(r.status, '?')}] {r.name}{ver}: {r.status}{detail}")


def _cmd_tools(args: argparse.Namespace) -> int:
    if args.tools_cmd == "install":
        results = _provision(force=args.force, names=args.names or None)
        _print_provision(results)
        return 0 if all(r.status != "failed" for r in results) else 1
    if args.tools_cmd == "status":
        from papaya_agent_runtime.setup.discovery import discover

        for c in discover()["companions"]:
            state = "available" if c["available"] else "missing"
            ver = f" {c['version']}" if c.get("version") else ""
            detail = f" ({c['detail']})" if c.get("detail") else ""
            print(f"  {c['name']:<11}{ver:<10} {state}{detail}")
        return 0
    print("no tools subcommand given", file=sys.stderr)
    return 2


#: The `[claude]` keys a person can lock against the runtime changing them.
_LOCKABLE_CLAUDE_KEYS = ("extra_tools", "dropped_tools", "allowed_tools")


def _config_capabilities(args: argparse.Namespace) -> int:
    """`ppy config capabilities`: this machine's grant policy for workers' requests."""
    from papaya_agent_runtime import capability_requests, config_changes
    from papaya_agent_runtime.config import ConfigError, load_config, save_config

    cfg = load_config()
    before = {
        "auto_grant": list(cfg.capabilities.auto_grant),
        "never": list(cfg.capabilities.never),
    }
    try:
        for raw in args.auto_grant:
            program = capability_requests.program_of(raw)
            if program in capability_requests.floor():
                print(f"`{program}` is never granted to a worker", file=sys.stderr)
                return 1
            cfg.capabilities.never = [p for p in cfg.capabilities.never if p != program]
            if program not in cfg.capabilities.auto_grant:
                cfg.capabilities.auto_grant.append(program)
        for raw in args.never:
            program = capability_requests.program_of(raw)
            cfg.capabilities.auto_grant = [p for p in cfg.capabilities.auto_grant if p != program]
            if program not in cfg.capabilities.never:
                cfg.capabilities.never.append(program)
        for raw in args.remove:
            program = capability_requests.program_of(raw)
            cfg.capabilities.auto_grant = [p for p in cfg.capabilities.auto_grant if p != program]
            cfg.capabilities.never = [p for p in cfg.capabilities.never if p != program]
    except capability_requests.CapabilityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    after = {"auto_grant": list(cfg.capabilities.auto_grant), "never": list(cfg.capabilities.never)}
    if after != before:
        try:
            save_config(cfg)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        config_changes.record(
            key="capabilities", before=before, after=after, why="changed by a person"
        )
    print(
        "granted without asking: the safe family" + "".join(f", {p}" for p in after["auto_grant"])
    )
    print("never granted: " + ", ".join(sorted(capability_requests.floor() | set(after["never"]))))
    print("anything else waits on a person: `ppy capability list`")
    return 0


def _config_claude(args: argparse.Namespace) -> int:
    """`ppy config claude`: edit the deltas against the code's profile, or show them."""
    from papaya_agent_runtime import config_changes
    from papaya_agent_runtime import health as _health
    from papaya_agent_runtime.config import (
        CLAUDE_PROFILE,
        ConfigError,
        claude_tool_provenance,
        load_config,
        normalise_tool,
        save_config,
    )

    cfg = load_config()
    claude = cfg.claude
    changes: list[dict] = []

    def change(key: str, before: object, after: object, what: str) -> None:
        if before != after:
            changes.append(
                {
                    "key": f"claude.{key}",
                    "before": before,
                    "after": after,
                    "why": f"set by a person: ppy config claude {what}",
                }
            )

    if args.reset:
        before = {"extra_tools": claude.extra_tools, "dropped_tools": claude.dropped_tools}
        if claude.allowed_tools is not None:
            before["allowed_tools"] = claude.allowed_tools
        claude.extra_tools, claude.dropped_tools, claude.allowed_tools = [], [], None
        change("tools", before, {"extra_tools": [], "dropped_tools": []}, "--reset")
    for raw in args.allow or []:
        pattern = normalise_tool(raw)
        extra, dropped = list(claude.extra_tools), list(claude.dropped_tools)
        claude.dropped_tools = [t for t in dropped if t != pattern]
        if pattern not in CLAUDE_PROFILE and pattern not in extra:
            claude.extra_tools = [*extra, pattern]
        change("dropped_tools", dropped, claude.dropped_tools, f"--allow {pattern}")
        change("extra_tools", extra, claude.extra_tools, f"--allow {pattern}")
    for raw in args.deny or []:
        pattern = normalise_tool(raw)
        extra, dropped = list(claude.extra_tools), list(claude.dropped_tools)
        claude.extra_tools = [t for t in extra if t != pattern]
        if pattern in CLAUDE_PROFILE and pattern not in dropped:
            claude.dropped_tools = [*dropped, pattern]
        change("extra_tools", extra, claude.extra_tools, f"--deny {pattern}")
        change("dropped_tools", dropped, claude.dropped_tools, f"--deny {pattern}")
    for key, locking in [(k, True) for k in args.lock or []] + [
        (k, False) for k in args.unlock or []
    ]:
        if key not in _LOCKABLE_CLAUDE_KEYS:
            raise ConfigError(
                f"cannot lock claude.{key}; lockable keys: {', '.join(_LOCKABLE_CLAUDE_KEYS)}"
            )
        before = list(claude.locked)
        if locking and key not in before:
            claude.locked = [*before, key]
        elif not locking:
            claude.locked = [k for k in before if k != key]
        change("locked", before, claude.locked, f"{'--lock' if locking else '--unlock'} {key}")

    if changes:
        save_config(cfg)
        for entry in changes:
            config_changes.record(**entry)
    print(_health.describe_claude_tools(_health.claude_tool_profile()))
    if args.show:
        for pattern, origin in claude_tool_provenance(cfg):
            print(f"  {origin:<8} {pattern}")
        if claude.locked:
            print(f"  locked: {', '.join(claude.locked)}")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.config import ConfigError, load_config
    from papaya_agent_runtime.setup.wizard import config_authority, config_models

    try:
        if args.config_cmd == "show":
            cfg = load_config()
            print(json.dumps(cfg.to_dict(), indent=2))
            return 0
        if args.config_cmd == "models":
            cfg = config_models(_collect_overrides(args))
            print(
                f"updated models: manager {cfg.manager.model}, "
                f"worker default {cfg.worker.default_model}@{cfg.worker.default_reasoning}, "
                f"ceiling {cfg.worker.max_model}@{cfg.worker.max_reasoning}, "
                f"concurrency {cfg.worker.max_concurrent}"
            )
            return 0
        if args.config_cmd == "authority":
            overrides: dict = {}
            if args.allow_merge is not None:
                overrides["merge"] = args.allow_merge
            cfg = config_authority(overrides)
            print(f"merge authority: {'on' if cfg.authority.merge else 'off'}")
            return 0
        if args.config_cmd == "claude":
            return _config_claude(args)
        if args.config_cmd == "capabilities":
            return _config_capabilities(args)
        if args.config_cmd == "delivery":
            from papaya_agent_runtime import config_changes
            from papaya_agent_runtime.config import save_config

            cfg = load_config()
            if args.merged_status is not None:
                before = cfg.delivery.merged_status
                cfg.delivery.merged_status = args.merged_status.strip()
                save_config(cfg)
                config_changes.record(
                    key="delivery.merged_status",
                    before=before,
                    after=cfg.delivery.merged_status,
                    why="this workspace said what a merged work item moves to",
                )
            rule = cfg.delivery.merged_status
            print(
                f"a merged pull request moves its work item to {rule}"
                if rule
                else "a merged pull request's work item is not moved: the runtime asks"
            )
            return 0
        if args.config_cmd == "history":
            from papaya_agent_runtime import config_changes

            entries = config_changes.history(limit=args.limit)
            if args.json:
                print(json.dumps(entries, indent=2))
                return 0
            if not entries:
                print("no configuration changes recorded")
            for entry in entries:
                print(
                    f"#{entry['id']} {entry['at']} {entry['key']}: "
                    f"{json.dumps(entry['before'])} -> {json.dumps(entry['after'])} — "
                    f"{entry['why']}"
                )
            return 0
        if args.config_cmd == "health":
            from papaya_agent_runtime.config import save_config

            cfg = load_config()
            if args.quiet_minutes is not None:
                cfg.health.quiet_minutes = args.quiet_minutes
            if args.plan_minutes is not None:
                cfg.health.plan_minutes = args.plan_minutes
            if args.max_stale_stacks is not None:
                cfg.health.max_stale_stacks = args.max_stale_stacks
            save_config(cfg)
            print(
                f"worker health: flag a worker after {cfg.health.quiet_minutes}m of silence, "
                f"or {cfg.health.plan_minutes}m without a plan; refuse dispatch above "
                f"{cfg.health.max_stale_stacks} stale compose stack(s)"
            )
            return 0
        if args.config_cmd == "assessments":
            cfg = load_config()
            policy = cfg.assessments
            for name in (
                "enabled",
                "completed_runs",
                "max_days",
                "cooldown_days",
                "minimum_runs",
                "failure_trigger_count",
                "max_actions",
            ):
                value = getattr(args, name, None)
                if value is not None:
                    setattr(policy, name, value)
            from papaya_agent_runtime.config import save_config

            save_config(cfg)
            print(
                "assessment cadence: "
                f"{'on' if policy.enabled else 'off'}, every {policy.completed_runs} completed "
                f"runs or {policy.max_days} days, {policy.cooldown_days}-day cooldown"
            )
            return 0
    except ConfigError as exc:
        print(f"config failed: {exc}", file=sys.stderr)
        return 1
    print("no config subcommand given", file=sys.stderr)
    return 2


def _cmd_repo(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.repos import (
        RepoError,
        add_repo,
        get_provision,
        get_settings,
        list_repos,
        set_provision,
        set_settings,
        sync_repo,
    )

    try:
        if args.repo_cmd == "set":
            from papaya_agent_runtime.environment import REPO_COLUMNS

            changes = {col: getattr(args, col, None) for col in REPO_COLUMNS}
            asked = args.migrations_glob is not None or any(
                value is not None for value in changes.values()
            )
            budget_changes = getattr(args, "budget", None) or []
            if args.default_branch is not None:
                from papaya_agent_runtime.repos import set_default_branch

                branch = set_default_branch(args.name, args.default_branch)
                if args.default_branch.strip():
                    print(
                        f"{args.name}: default branch pinned to {branch} (the forge's HEAD no "
                        "longer decides; an empty --default-branch unpins it)"
                    )
                else:
                    print(f"{args.name}: default branch follows the forge again ({branch or '?'})")
                if not asked and not budget_changes:
                    return 0
            if budget_changes and not asked:
                return _repo_set_budgets(args.name, budget_changes)
            settings = (
                set_settings(args.name, migrations_glob=args.migrations_glob, **changes)
                if asked
                else get_settings(args.name)
            )
            print(settings.describe())
            return _repo_set_budgets(args.name, budget_changes) if budget_changes else 0
        if args.repo_cmd == "show":
            settings = get_settings(args.name)
            print(args.name)
            if settings.environment is not None:
                print("\n".join(f"  {line}" for line in settings.environment.gate_lines()))
            print(settings.describe())
            return 0
        if args.repo_cmd == "budgets":
            return _repo_budgets(args.name)
        if args.repo_cmd == "provision":
            asked = args.clear or args.command is not None or args.reuse_venv is not None
            settings = (
                set_provision(
                    args.name,
                    command=args.command,
                    reuse_venv=args.reuse_venv,
                    clear=args.clear,
                )
                if asked
                else get_provision(args.name)
            )
            print(settings.describe())
            return 0
        if args.repo_cmd == "add":
            added = add_repo(args.url, name=args.name, forge_url=args.forge_url)
            short_sha = added.base_sha[:8] if added.base_sha else "?"
            print(
                f"registered {added.name} from {added.origin} "
                f"(branch {added.default_branch}, base {short_sha}, "
                f"forge {added.forge_url})"
            )
            for note in added.notes:
                print(f"  {note}")
            return 0
        if args.repo_cmd == "list":
            repos = list_repos()
            if not repos:
                print("no repositories registered")
                return 0
            for r in repos:
                sha = (r.get("base_sha") or "?")[:8]
                forge = r.get("forge_url") or "NO FORGE — deliver cannot open PRs"
                print(
                    f"{r['name']:<24} {r['default_branch'] or '?':<12} {sha}  "
                    f"{r['origin']}  ->  {forge}"
                )
            return 0
        if args.repo_cmd == "sync":
            res = sync_repo(args.name, clean_stray_ppy=args.clean_stray_ppy)
            if res.fast_forwarded:
                previous = (res.previous_sha or "?")[:8]
                print(
                    f"synced {args.name}: fast-forwarded {res.default_branch} "
                    f"{previous} -> {res.base_sha[:8]}"
                )
            else:
                print(
                    f"synced {args.name}: {res.default_branch} already at "
                    f"{res.base_sha[:8]} (nothing to fast-forward)"
                )
            for note in res.notes:
                print(f"  {note}")
            return 0
        if args.repo_cmd == "discover":
            return _repo_discover(args)
        if args.repo_cmd == "onboard":
            return _repo_onboard(args)
        if args.repo_cmd == "ensure":
            return _repo_ensure(args)
        if args.repo_cmd == "locate":
            return _repo_locate(args)
    except RepoError as exc:
        print(f"repo error: {exc}", file=sys.stderr)
        return 1
    print("no repo subcommand given", file=sys.stderr)
    return 2


def _repo_budgets(name: str | None) -> int:
    """`ppy repo budgets [<repo>]`: each kind's observations, p90, budget, and its source."""
    from papaya_agent_runtime import budgets
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        names = [name] if name else [str(r["name"]) for r in store.list_repos(conn)]
        if name and store.get_repo(conn, name) is None:
            print(f"repo error: repo {name!r} is not registered", file=sys.stderr)
            return 1
        if not names:
            print("no repositories registered")
            return 0
        print(
            "budget = p90 of the newest "
            f"{budgets.WINDOW_COUNT} observations within {budgets.WINDOW_DAYS} days "
            f"x {budgets.FACTOR:g}, floored at the default and capped; stalls and kills "
            "left out; an override wins"
        )
        for repo in names:
            print(
                budgets.render(
                    repo,
                    budgets.all_budgets(repo, conn=conn),
                    budgets.memory(repo, conn=conn),
                )
            )
    finally:
        conn.close()
    return 0


def _repo_set_budgets(name: str, values: list[str]) -> int:
    """`ppy repo set <name> --budget <kind>=<seconds>`: a person's word wins over the history."""
    from papaya_agent_runtime import budgets
    from papaya_agent_runtime.state import init_db, store

    try:
        parsed = [budgets.parse_override(value) for value in values]
    except budgets.BudgetError as exc:
        print(f"repo error: {exc}", file=sys.stderr)
        return 1
    conn = init_db()
    try:
        if store.get_repo(conn, name) is None:
            print(f"repo error: repo {name!r} is not registered", file=sys.stderr)
            return 1
        for kind, seconds in parsed:
            budgets.set_override(conn, name, kind, seconds)
    finally:
        conn.close()
    return _repo_budgets(name)


def _repo_ensure(args: argparse.Namespace) -> int:
    """Make a repo ready to work in, registering it on demand when it is theirs."""
    from papaya_agent_runtime.solicit import NotYours, SolicitError, ensure

    try:
        result = ensure(args.repo, allow_outside=args.allow_outside)
    except NotYours as exc:
        print(f"not registered: {exc}", file=sys.stderr)
        return 2
    except SolicitError as exc:
        print(f"could not take it on: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.__dict__, indent=2))
        return 0
    print(result.sentence())
    if result.notes_path:
        print(f"  what it builds and tests is in {result.notes_path}")
    return 0


def _repo_discover(args: argparse.Namespace) -> int:
    """Offer repositories from the forge that are not registered yet."""
    from papaya_agent_runtime.solicit import SolicitError, candidates

    try:
        found = candidates(owner=args.owner, limit=args.limit, include_forks=args.include_forks)
    except SolicitError as exc:
        print(f"discovery unavailable: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([c.__dict__ for c in found], indent=2))
        return 0
    if not found:
        print("nothing to offer: every repository you can see is already registered")
        return 0
    for candidate in found[: args.top]:
        print(candidate.sentence())
    remaining = len(found) - min(len(found), args.top)
    if remaining > 0:
        print(f"... and {remaining} more")
    return 0


def _repo_locate(args: argparse.Namespace) -> int:
    """Which registered clones contain a ticket's distinctive strings. No judgment."""
    from papaya_agent_runtime.repos import locate

    hits = locate(list(args.terms))
    if args.json:
        print(json.dumps([{**hit.__dict__, "found": hit.found} for hit in hits], indent=2))
        return 0
    if not hits:
        print("no repositories registered, so there is nowhere to look")
        return 0
    for hit in hits:
        if hit.note:
            print(f"{hit.repo}: not searched — {hit.note}")
            continue
        if not hit.found:
            print(f"{hit.repo}: no hits")
            continue
        print(f"{hit.repo}: {hit.matches} hit(s) in {hit.file_count} file(s)")
        for name in hit.files:
            print(f"  {name}")
        if hit.file_count > len(hit.files):
            print(f"  ... and {hit.file_count - len(hit.files)} more")
    return 0


def _repo_onboard(args: argparse.Namespace) -> int:
    """Read a registered repository and record how it builds, tests and gates."""
    from papaya_agent_runtime.solicit import SolicitError, inspect, onboard, render_notes

    try:
        if args.dry_run:
            print(render_notes(inspect(args.name)), end="")
            return 0
        report, path = onboard(args.name, local_gate=args.local_gate)
    except SolicitError as exc:
        print(f"onboarding failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        payload = {**report.__dict__, "notes_path": str(path)}
        payload["gate"] = dataclasses.asdict(report.gate) if report.gate is not None else None
        print(json.dumps(payload, indent=2))
        return 0
    stack = ", ".join(report.stacks) or "stack not recognised"
    verified = report.commands.get("test") or "no test command found"
    print(f"onboarded {report.name}: {stack}; verify with `{verified}`")
    if report.gate is not None:
        for line in report.gate.describe():
            print(f"  gate policy: {line}")
    if report.ci_commands:
        print(f"  CI runs {len(report.ci_commands)} command(s); the gate is in {path}")
    for unknown in report.unknowns:
        print(f"  still unknown: {unknown}")
    print(f"  written to {path}")
    return 0


def _cmd_readiness(args: argparse.Namespace) -> int:
    """Can this runtime take work, and if not, whose problem is each piece?"""
    from papaya_agent_runtime import capabilities
    from papaya_agent_runtime import readiness as _readiness
    from papaya_agent_runtime.state import init_db

    verdict = _readiness.check()

    if args.report:
        print(_readiness.report(verdict, agent=args.agent or "", where=args.where or ""))
    elif args.json:
        data = verdict.as_dict()
        data["client"] = {
            "version": capabilities.client_version(),
            "protocol": capabilities.protocol(),
        }
        conn = init_db()
        data["already_reported"] = _readiness.already_reported(conn, verdict)
        print(json.dumps(data, indent=2))
    else:
        print(f"{verdict.state}: {_readiness.headline(verdict)}")
        print(f"  client: {capabilities.client_line()}")
        for problem in verdict.problems:
            mark = "BLOCKS" if problem.blocking else "info  " if problem.info else "gap   "
            who = "you" if problem.owner == _readiness.USER else "me"
            print(f"  {mark} [{who}] {problem.summary}")
            print(f"         fix: {problem.fix}")

    if args.mark_reported:
        _readiness.mark_reported(init_db(), verdict)
    if args.forget:
        _readiness.forget_reports(init_db())

    return 1 if verdict.state == _readiness.BLOCKED else 0


def _cmd_blockers(args: argparse.Namespace) -> int:
    """What this machine needs from a person, with the commands that do it."""
    from papaya_agent_runtime import blockers
    from papaya_agent_runtime import readiness as _readiness

    found = blockers.from_verdict(_readiness.check())
    if args.json:
        print(json.dumps({"blockers": found}, indent=2))
    else:
        print(blockers.render_text(found))
    return 1 if found else 0


def _cmd_track(args: argparse.Namespace) -> int:
    """Record which tracker record a task belongs to, wherever that tracker is."""
    from papaya_agent_runtime import tracker
    from papaya_agent_runtime.state import init_db

    if not args.show and not args.record:
        print("give --record <id>, or --show to read what is already recorded", file=sys.stderr)
        return 2
    conn = init_db()
    if args.show:
        link = tracker.task_link(conn, args.task)
        print(tracker.link_sentence(link) if link else f"task {args.task} is not tracked anywhere")
        return 0 if link else 1
    tracker.link_task(
        conn,
        args.task,
        record=args.record,
        provider=args.provider,
        url=args.url or "",
        title=args.title or "",
    )
    print(tracker.link_sentence(tracker.task_link(conn, args.task)))
    return 0


def _cmd_papaya(args: argparse.Namespace) -> int:
    """The Papaya connection: who this runtime is, and establishing that."""
    from papaya_agent_runtime import papaya

    if args.papaya_cmd == "status":
        data = papaya.status()
        if args.json:
            print(json.dumps(data, indent=2))
            return 0
        print(_papaya_status_line(data))
        return 0 if data["state"] == "connected" else 1

    if args.papaya_cmd == "connect":
        result = papaya.connect(harness=args.harness)
        if result["ok"]:
            print(f"connected as {result['status']['addressed']}")
            return 0
        print(
            f"not connected ({result['reason']}): {result['detail']} — "
            "the runtime still works on registered repositories without Papaya",
            file=sys.stderr,
        )
        return 1

    if args.papaya_cmd == "tools":
        from papaya_agent_runtime.manager.launch import repo_root

        root = repo_root()
        if args.check:
            ready = papaya.session_tools_ready(root)
            print(
                "Papaya tools are configured for sessions here"
                if ready
                else "sessions here have no Papaya tools: `ppy papaya tools` adds them"
            )
            return 0 if ready else 1
        result = papaya.install_session_tools(root)
        if args.json:
            print(json.dumps(result, indent=2))
        elif result["ok"]:
            print(f"{result['addressed']}: {result['detail']}")
        else:
            print(f"not configured ({result['reason']}): {result['detail']}", file=sys.stderr)
        return 0 if result["ok"] else 1

    if args.papaya_cmd == "context":
        payload = papaya.context(refresh=args.refresh)
        if payload is None:
            print("no Papaya context available; this machine is not connected", file=sys.stderr)
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    print("no papaya subcommand given", file=sys.stderr)
    return 2


def _papaya_status_line(data: dict) -> str:
    """One sentence describing the connection, written for a person."""
    state = data["state"]
    if state == "connected":
        who = data["identity"] or {}
        role = who.get("role_label") or ""
        suffix = f" ({role})" if role else ""
        return f"connected as {data['addressed']}{suffix}"
    if state == "signed_in":
        return "signed in to Papaya but not pinned to an agent — `ppy papaya connect` finishes it"
    if state == "installed":
        return "the Papaya client is installed but this machine is not signed in"
    return "no Papaya client on this machine; `ppy papaya connect` installs and connects one"


def _cmd_supervisor(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable
    from papaya_agent_runtime.supervisor.server import SupervisorServer, default_socket_path

    if args.supervisor_cmd == "serve":
        import signal

        from papaya_agent_runtime.supervisor import lifeline
        from papaya_agent_runtime.supervisor.server import SupervisorOwned

        server = SupervisorServer()
        try:
            server._bind()
        except SupervisorOwned as exc:
            print(f"refusing to start: {exc}", file=sys.stderr)
            return 1
        lifeline.start()

        def terminated(_signum, _frame) -> None:
            raise KeyboardInterrupt

        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, terminated)
        print(f"supervisor listening on {server.socket_path} (Ctrl-C to stop)")
        try:
            server._serve_loop()
        except KeyboardInterrupt:
            print("supervisor stopping: waiting for workers to be recorded stopped")
        finally:
            # Stopped by a signal or by `ppy supervisor stop`: what it started goes with it.
            server.shutdown()
            lifeline.stop()
        print("supervisor stopped")
        return 0

    client = SupervisorClient()
    try:
        if args.supervisor_cmd == "status":
            resp = client.ping()
            print(f"supervisor up (pid {resp.get('pid')}) at {default_socket_path()}")
            return 0
        if args.supervisor_cmd == "stop":
            answer = client.shutdown()
            wait = answer.get("stop_timeout")
            print(
                "supervisor shutdown requested"
                + (
                    f"; it stops its workers (up to {wait:g}s, sessions kept) and exits"
                    if isinstance(wait, int | float)
                    else ""
                )
            )
            return 0
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def _cmd_serve(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.serve import serve

    return serve(list(getattr(args, "listen_args", None) or []))


def _cmd_sweep(args: argparse.Namespace) -> int:
    """Ask the running `ppy serve` to look for assigned work now, and say what it found."""
    from papaya_agent_runtime.paths import ppy_home
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    def unpicked() -> int:
        # No serve to offer work to: say what is assigned and waiting, with the same
        # filters the sweep applies, so a session takes it up itself.
        from papaya_agent_runtime import supervision

        items = supervision.assigned_unpicked()
        if getattr(args, "json", False):
            print(json.dumps(items, indent=2, sort_keys=True, default=str))
            return 0
        if not items:
            print(f"nothing is serving in {ppy_home()}, and no assigned work is waiting")
        for item in items:
            key = item.get("display_id") or item.get("key") or item.get("id")
            print(f"assigned and waiting: {key} {item.get('title') or ''}".rstrip())
        return 0

    try:
        resp = SupervisorClient().sweep(include_declined=bool(args.include_declined))
    except SupervisorUnavailable:
        return unpicked()
    if not resp.get("serving", True):
        return unpicked()
    if not resp.get("ok"):
        print(f"sweep failed: {resp.get('error')}", file=sys.stderr)
        return 1
    result = resp.get("sweep") or {}
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result.get("summary") or "sweep finished")
    return 1 if result.get("error") else 0


def _cmd_deficiency(args: argparse.Namespace) -> int:
    """The runtime's own deficiencies, and the issues `ppy serve` opened about them."""
    from dataclasses import asdict

    from papaya_agent_runtime import deficiencies, tool_learning

    rows = deficiencies.ledger(include_all=bool(args.all))
    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
        return 0
    if not rows:
        print(
            "no deficiencies recorded"
            + ("" if args.all else " at or past their threshold (`--all` shows every one)")
        )
    for row in rows:
        where = row.issue_url or {
            deficiencies.PENDING: "waiting to open",
            deficiencies.RECLASSIFIED: "re-classified; not a profile gap",
        }.get(row.status, "below threshold")
        print(f"{row.status:<9} {row.count:>3}x  {row.last_seen}  {row.title}")
        print(f"{'':<16}{row.kind} {row.fingerprint} — {where}")
    # Denials that are never issues are still counted, so a person can see them.
    for repo, kinds in sorted(tool_learning.counts().items()):
        quiet = [
            f"{kinds[k]} {k}"
            for k in (tool_learning.COMMAND_SHAPE, tool_learning.POLICY_REFUSAL)
            if kinds.get(k)
        ]
        if quiet:
            print(f"worker denials in {repo}, never reported: {', '.join(quiet)}")
    return 0


def _registered_repo_origin(repo: str) -> str | None:
    """The base clone's ``origin`` URL for a registered repo, or None if unknowable.

    This is the remote a worker's worktree inherits, so it is the one a warning
    about where a push would land has to name. A repo that is not registered (or
    a state tree that cannot be opened) simply yields no answer: the dispatch
    itself reports that, and a warning must never be the thing that fails.
    """
    try:
        from papaya_agent_runtime import repos
        from papaya_agent_runtime.state import init_db, store

        row = store.get_repo(init_db(), repo)
        if row is None:
            return None
        return repos.remote_url(row["local_path"])
    except Exception:
        return None


def _cmd_brief(args: argparse.Namespace) -> int:
    """`ppy brief lint <file>`: what a worker cannot recover from in this brief."""
    from papaya_agent_runtime import brief_lint, preflight

    try:
        text = preflight.read_brief(args.file)
    except preflight.PreflightError as exc:
        print(f"brief lint: {exc}", file=sys.stderr)
        return 1
    findings = brief_lint.lint_brief(text, ends_at=args.ends_at)
    kind = "defect brief" if brief_lint.is_defect_brief(text) else "brief"
    if not findings:
        print(f"{args.file}: {kind}, no findings")
        return 0
    print(brief_lint.render(findings, args.file))
    print(f"{args.file}: {kind}, {len(findings)} finding(s)")
    return 1


def _ticket_run_id() -> int | None:
    """The run of the ticket this shell's manager turn is holding, if it is one.

    `ppy serve` exports it into every manager turn, and a worker dispatched into
    that run is how the runner sees the turn did its job. Defaulting to it here
    means the link does not rest on a turn remembering a flag.
    """
    from papaya_agent_runtime.papaya_events import TICKET_RUN_ENV

    raw = (os.environ.get(TICKET_RUN_ENV) or "").strip()
    return int(raw) if raw.isdigit() else None


def _cmd_dispatch(args: argparse.Namespace) -> int:
    from papaya_agent_runtime import brief_lint, health, preflight
    from papaya_agent_runtime.config import default_worker_provider
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    try:
        health.require_dispatch_capacity(args.repo)
    except health.DispatchHealthError as exc:
        print(f"dispatch refused: {exc}", file=sys.stderr)
        return 1

    # Preflight first: refuse before any task state exists, and say why plainly.
    if args.brief and args.instructions:
        print("dispatch: pass --brief <file> or --instructions, not both", file=sys.stderr)
        return 1
    if not args.title and not args.brief:
        print(
            "dispatch: name the objective with --title, or pass --brief <file> and let the "
            "brief's first Markdown heading name it",
            file=sys.stderr,
        )
        return 1
    try:
        instructions = preflight.read_brief(args.brief) if args.brief else (args.instructions or "")
        preflight.check_disk()
    except preflight.PreflightError as exc:
        print(f"dispatch refused: {exc}", file=sys.stderr)
        return 1
    if args.brief:
        findings = brief_lint.lint_brief(instructions, ends_at=args.ends_at)
        if findings:
            # Wrong premises and self-contradicting scope cost 25 and 13 reflections
            # in cycle 4 (issue #59); say so before the worker is out the door.
            print(f"brief lint: {len(findings)} finding(s) in {args.brief}", file=sys.stderr)
            print(brief_lint.render(findings, args.brief), file=sys.stderr)
            if args.strict:
                print(
                    "dispatch refused: --strict and the brief lint found problems; fix the "
                    "brief (see `ppy brief lint`) or dispatch without --strict",
                    file=sys.stderr,
                )
                return 1

    title = args.title
    if not title:
        title = preflight.title_from_brief(instructions)
        if not title:
            print(
                f"dispatch: {args.brief} has no Markdown heading to take the objective from — "
                "open the brief with a '# ...' heading, or pass --title",
                file=sys.stderr,
            )
            return 1
        print(f'objective taken from the brief\'s first heading: "{title}"')

    # An omitted --provider means "whatever this instance runs workers as", never
    # the stub provider: defaulting to `fake` sent a real task to a fake worker on
    # 2026-09-04, which pushed a stub branch to a live GitHub remote (issue #49).
    provider = args.provider or default_worker_provider()
    if provider == "fake":
        from papaya_agent_runtime import repos

        origin = _registered_repo_origin(args.repo)
        if origin and not repos.is_local_remote(origin):
            print(
                f"warning: --provider fake on {args.repo}, whose origin is {origin} — the fake "
                "worker writes a stub and refuses to push to a remote that is not a local path; "
                "pass --provider claude or --provider codex for real work"
            )

    run_id = args.run_id if args.run_id is not None else _ticket_run_id()

    client = SupervisorClient()
    try:
        resp = client.dispatch_task(
            repo=args.repo,
            title=title,
            instructions=instructions,
            provider=provider,
            model=args.model,
            reasoning=args.reasoning,
            run_id=run_id,
            base=args.base,
            stack_on=args.stack_on,
            ends_at=args.ends_at,
        )
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"dispatch failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"dispatched task {resp['task_id']} in run {resp['run_id']} (branch {resp['branch']})")
    for advisory in ("migration_advisory", "overlap_advisory"):
        if resp.get(advisory):
            print(resp[advisory])
    if resp.get("compose_project"):
        port = f" on port {resp['db_port']}" if resp.get("db_port") else ""
        print(
            f"environment: compose project {resp['compose_project']}{port}; receipts go "
            f"under {resp.get('evidence_path')}"
        )
    elif resp.get("evidence_path"):
        print(f"environment: receipts go under {resp['evidence_path']}")
    if instructions.strip():
        # Archived whether the instructions came from a file or the command line:
        # this is the copy `ppy deliver` quotes when it writes the pull request body,
        # and the only durable record of the packet a worker actually received.
        archived = preflight.archive_brief(args.repo, int(resp["task_id"]), instructions)
        print(f"brief archived at {archived}")
    return 0


def _cmd_worktree(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.worktree.reclaim import human_bytes, list_worktrees, prune

    if args.worktree_cmd == "list":
        entries = list_worktrees(args.repo)
        if args.json:
            print(json.dumps([vars(e) for e in entries], indent=2))
            return 0
        if not entries:
            print("no leased or orphaned worktrees")
            return 0
        print(f"{'SLOT':<14}{'TASK':<7}{'STATUS':<14}{'STATE':<10}{'SIZE':>10}  BRANCH")
        for e in entries:
            state = "gone" if not e.exists else ("dirty" if e.dirty else "clean")
            if e.exists and not e.dirty and e.unpushed_commits > 0:
                state = f"+{e.unpushed_commits} local"
            task = str(e.task_id) if e.task_id else "-"
            if e.orphaned:
                status = "orphaned" if e.managed else "unmanaged"
            else:
                status = e.task_status or "?"
            print(
                f"{e.slot:<14}{task:<7}{status:<14}{state:<10}"
                f"{human_bytes(e.size_bytes):>10}  {e.branch}"
            )
        total = sum(e.size_bytes for e in entries)
        free = sum(e.size_bytes for e in entries if e.reclaimable)
        foreign = [e for e in entries if e.orphaned and not e.managed]
        orphans = [e for e in entries if e.orphaned and e.managed]
        line = f"{len(entries)} slot(s), {human_bytes(total)} on disk, {human_bytes(free)} prunable"
        if orphans:
            line += (
                f" — {len(orphans)} of them orphaned "
                f"({human_bytes(sum(e.size_bytes for e in orphans))}, no active lease owns them)"
            )
        print(line)
        if foreign:
            print(
                f"{len(foreign)} slot(s) in those pools belong to a checkout this instance "
                "does not manage — listed as unmanaged, never counted, never pruned"
            )
        return 0

    res = prune(args.repo, dry_run=args.dry_run)
    verb = "would remove" if res["dry_run"] else "removed"
    for r in res["removed"]:
        print(f"  {verb} {r['path']} ({human_bytes(r['size_bytes'])}) — {r['reason']}")
    for r in res["skipped"]:
        print(f"  kept {r['path']} ({human_bytes(r['size_bytes'])}) — {r['reason']}")
    from papaya_agent_runtime import compose

    for stack in res.get("compose") or []:
        line = compose.describe(stack)
        if line:
            print(f"  {line}")
    print(
        f"{verb} {len(res['removed'])} worktree(s), "
        f"{human_bytes(res['reclaimed_bytes'])} reclaimed; "
        f"{len(res['skipped'])} kept ({human_bytes(res['held_bytes'])})"
    )
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.wait_actionable(args.run_id, timeout=args.timeout, after_seq=args.after_seq)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if resp.get("timed_out"):
        print(f"run {args.run_id}: no actionable state within {args.timeout}s")
        return 0
    print(json.dumps(resp, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Non-blocking snapshot of a run: task states, actionable events, usage.

    Reads the supervisor's live state (workers run in the background daemon) and
    returns immediately — it never waits. Use this to track progress inside a turn
    instead of the blocking ``ppy wait``.
    """
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        snap = client.run_status(args.run_id)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not snap.get("ok", True) or "run" not in snap:
        print(f"run {args.run_id}: {snap.get('error', 'not found')}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    run = snap["run"]
    print(f'run {run["id"]} "{run.get("title", "")}" — {run.get("status", "?")}')
    for t in snap.get("tasks", []):
        prov = t.get("provider") or "?"
        print(f'  task {t["id"]} "{t.get("title", "")}" [{prov}] {t.get("status", "?")}')
    actionable = snap.get("actionable", [])
    if actionable:
        marks = ", ".join(f"{e['kind']}(seq {e['seq']})" for e in actionable)
        print(f"actionable: {marks}")
        # An event that names a reason is worth more than its kind: a worker whose
        # turn ended mid-gate has to say so here, or it reads as finished work.
        for event in actionable:
            payload = event.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            summary = (payload or {}).get("summary") if isinstance(payload, dict) else None
            if event["kind"] == "worker_stopped" and summary:
                print(f"  task {(payload or {}).get('task_id')}: {summary}")
    else:
        print("actionable: none")
    usage = snap.get("usage") or {}
    if usage:
        print(f"usage: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}")
    return 0


def _cmd_task(args: argparse.Namespace) -> int:
    """Snapshot a task, or run one of the lifecycle bookkeeping commands."""
    from papaya_agent_runtime import compose
    from papaya_agent_runtime.lifecycle import LifecycleError, close_task, set_task_status

    try:
        if args.task_cmd == "env":
            return _cmd_task_env(args)
        if args.task_cmd == "close":
            res = close_task(args.task_id, args.reason)
            lease = res.get("lease") or {}
            print(
                f"task {args.task_id}: {res['previous_status']} -> closed "
                f"({res['reason']}); {lease.get('note', 'no lease on record')}"
            )
            line = compose.describe(res.get("compose"))
            if line:
                print(line)
            return 0
        if args.task_cmd == "push":
            return _cmd_task_push(args)
        if args.task_cmd == "set-status":
            res = set_task_status(args.task_id, args.status, args.note)
            note = f" — {args.note}" if args.note else ""
            print(f"task {args.task_id}: {res['from']} -> {res['to']}{note}")
            return 0
    except LifecycleError as exc:
        print(f"task {args.task_cmd}: {exc}", file=sys.stderr)
        return 1
    return _cmd_task_show(args)


def _cmd_task_push(args: argparse.Namespace) -> int:
    """Push a task's lease branch by hand: the manual form of the harness's rescue.

    The supervisor does this itself when a worker files a done note it could not
    push. This is the same push for every other case — a task whose rescue was
    refused and whose hook has since been fixed, or a worktree the manager wants
    on the remote before reviewing it.
    """
    from papaya_agent_runtime import turn_end
    from papaya_agent_runtime.state import init_db

    result = turn_end.push_lease_branch(init_db(), args.task_id)
    if not result.pushed:
        print(f"task {args.task_id}: {result.note}", file=sys.stderr)
        return 1
    print(f"task {args.task_id}: {result.note}")
    return 0


def _cmd_task_env(args: argparse.Namespace) -> int:
    """Small facts attached to a task — today, the compose stack it owns."""
    from papaya_agent_runtime import compose
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    if store.get_task(conn, args.task_id) is None:
        print(f"task {args.task_id} not found", file=sys.stderr)
        return 1
    if args.task_env_cmd == "set":
        for assignment in args.assignments:
            key, sep, value = assignment.partition("=")
            if not sep or not key.strip():
                print(f"expected key=value, got {assignment!r}", file=sys.stderr)
                return 1
            key, value = key.strip(), value.strip()
            if key == compose.COMPOSE_PROJECT_KEY:
                try:
                    compose.record_project(args.task_id, value, conn=conn)
                except compose.ComposeError as exc:
                    print(f"task env set: {exc}", file=sys.stderr)
                    return 1
            else:
                store.set_task_env(conn, args.task_id, key, value)
            print(f"task {args.task_id}: {key} = {value}")
        return 0

    rows = store.task_env(conn, args.task_id)
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    if not rows:
        print(f"task {args.task_id}: nothing recorded")
        return 0
    for row in rows:
        print(f"{row['key']} = {row['value']}  ({row['source']}, {row['updated_at']})")
    return 0


def _cmd_task_show(args: argparse.Namespace) -> int:
    """Non-blocking snapshot of a single task. Returns immediately; never waits."""
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.task_status(args.task_id)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    task = resp.get("task")
    if not task:
        print(f"task {args.task_id}: {resp.get('error', 'not found')}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(task, indent=2))
        return 0
    prov = task.get("provider") or "?"
    model = task.get("model")
    label = f"{prov}/{model}" if model else prov
    line = f'task {task["id"]} "{task.get("title", "")}" [{label}] {task.get("status", "?")}'
    if task.get("branch"):
        line += f"  branch {task['branch']}"
    print(line)
    from papaya_agent_runtime import progress, turn_end
    from papaya_agent_runtime.state import init_db

    if task.get("status") == turn_end.WORKER_STOPPED:
        verdict = turn_end.latest_stop_verdict(init_db(), args.task_id)
        if verdict is not None:
            print(f"stopped: {verdict.summary}")
            print("resume with `ppy resume` (no --message needed) to send it back with that reason")
    print(f"progress: {progress.describe(progress.latest(args.task_id))}")
    return 0


def _cmd_stack(args: argparse.Namespace) -> int:
    """Render, merge, or rebuild a native pull-request stack."""
    from papaya_agent_runtime.stacks import (
        StackError,
        merge_stack,
        rebuild_layer,
        render_stack,
        stack_layers,
    )

    try:
        if args.command_or_id == "merge":
            if args.id is None:
                raise StackError("merge needs a task id")
            result = merge_stack(args.id, all_layers=args.all)
            for merged in result.merged:
                print(
                    f"task {merged['task_id']}: PR #{merged['pr']} merged at "
                    f"{merged['merge_commit'][:8]} and recorded"
                )
            for changed in result.retargeted:
                print(
                    f"task {changed['task_id']}: PR #{changed['pr']} retargeted to "
                    f"{changed['base']}"
                )
            if result.stopped:
                print(f"stack merge stopped: {result.stopped}", file=sys.stderr)
                return 1
            return 0
        if args.command_or_id == "rebuild":
            if args.id is None:
                raise StackError("rebuild needs a task id")
            rebuilt = rebuild_layer(args.id)
            print(f"task {args.id}: {rebuilt['note']}")
            return 0
        if args.id is not None:
            raise StackError("view takes one task or run id")
        try:
            identifier = int(args.command_or_id)
        except ValueError as exc:
            raise StackError("expected a task/run id, `merge <task>`, or `rebuild <task>`") from exc
        layers = stack_layers(identifier)
    except StackError as exc:
        print(f"stack: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([vars(layer) for layer in layers], indent=2))
        return 0
    print(render_stack(layers))
    return 0


def _cmd_lease(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.lifecycle import LifecycleError, release_task_lease

    try:
        res = release_task_lease(args.task_id, reason=args.reason, remove_branch=args.remove_branch)
    except LifecycleError as exc:
        print(f"lease release: {exc}", file=sys.stderr)
        return 1
    print(f"task {args.task_id}: {res['note']}")
    return 0


def _actor() -> str:
    """Who this `ppy` is acting for, as the `by` on what it records.

    A manager turn `ppy serve` launched carries its ticket's run in the environment;
    anything else on this machine is a person at a session.
    """
    from papaya_agent_runtime.papaya_events import TICKET_RUN_ENV
    from papaya_agent_runtime.state import store

    return store.BY_MANAGER if os.environ.get(TICKET_RUN_ENV) else store.BY_PERSON


def _cmd_resume(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.resume_task(
            args.task_id, message=args.message, ends_at=args.ends_at, by=_actor()
        )
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"resume failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(
        f"resuming task {args.task_id} (session {resp.get('resumed_session')}) — "
        f"status {resp.get('status', 'in_progress')} (ends at {resp.get('ends_at', args.ends_at)})"
    )
    return 0


def _cmd_steer(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    replace = args.replace or getattr(args, "stop", False)
    try:
        resp = client.steer_task(
            args.task_id, args.message, delivery="replace" if replace else "append", by=_actor()
        )
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    what = "stop" if getattr(args, "stop", False) else "steer"
    if not resp.get("ok"):
        print(f"{what} failed: {resp.get('error')}", file=sys.stderr)
        return 1
    status = resp.get("status")
    line = f"{what} task {args.task_id}: mode={resp.get('mode')}"
    if status:
        line += f" status={status}"
    print(f"{line} {resp.get('note', '')}".rstrip())
    for item in resp.get("queue") or []:
        print(
            f"  steer event {item['event']} ({item['delivery']}, will be {item['will_be']}): "
            f"{item['preview']}"
        )
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.answer_question(
            args.task_id, args.answer, scope=args.scope, rationale=args.rationale, by=_actor()
        )
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"answer failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(
        f"recorded decision {resp.get('decision_id')} ({args.scope}); resuming task {args.task_id}"
    )
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.review import (
        ReviewError,
        approval_note,
        build_bundle,
        is_approved_at_head,
        record_review,
    )

    try:
        if args.review_cmd == "show":
            from papaya_agent_runtime import captures, progress
            from papaya_agent_runtime.state import init_db, store

            bundle = build_bundle(args.task_id)
            # The worker's own report comes first, in full: judging a diff without
            # the evidence the worker filed is how a complete report was once read
            # as "missing".
            reports = progress.history(args.task_id)
            latest = progress.latest(args.task_id)
            if latest is not None:
                print(
                    f"worker report ({latest['phase']}, {len(reports)} report(s); "
                    f"full log: ppy progress {args.task_id}):"
                )
                print(latest["note"] or "(no note)")
                print()
            # The receipts the worker named, listed so nobody approves a visual
            # change on the strength of a sentence about screenshots.
            conn = init_db()
            task = store.get_task(conn, args.task_id)
            paths = captures.capture_paths(r["note"] for r in reports)
            if task is not None:
                from papaya_agent_runtime import health

                for advisory in health.usage_advisories(conn, task_id=args.task_id):
                    print(health.describe_usage_advisory(advisory))
                    print()
                # The evidence directory the environment block pinned is listed
                # whether or not the worker's report named it: a receipt filed
                # where the block said is never invisible to the reviewer.
                from papaya_agent_runtime import environment

                repo_row = conn.execute(
                    "SELECT * FROM repos WHERE id = ?", (task["repo_id"],)
                ).fetchone()
                evidence_path = environment.evidence_path_for(repo_row, task["worktree_path"])
                if evidence_path and os.path.isdir(evidence_path) and evidence_path not in paths:
                    paths.insert(0, evidence_path)
            listing = captures.render(
                paths, root=task["worktree_path"] if task is not None else None
            )
            if listing:
                print(listing)
                print()
            note = approval_note(args.task_id)
            if note:
                print(f"note on the standing approval: {note}")
                print()
            # Two unmerged tasks adding a migration off one revision is a red
            # migration graph after the second merge, and it is invisible in a
            # diff read on its own.
            if task is not None:
                from papaya_agent_runtime import migrations

                flag = migrations.review_flag(init_db(), task)
                if flag:
                    print(flag)
                    print()
                # Where this layer sits in its stack and what has to merge before
                # it — the line ten reflections said the brief should have carried.
                from papaya_agent_runtime import stacks

                order = stacks.merge_order(init_db(), task)
                if order:
                    print(order)
                    print()
            print(
                f"task {bundle.task_id}: {bundle.base_sha[:8]}..{bundle.head_sha[:8]} "
                f"({bundle.files_changed} file(s))"
            )
            if getattr(bundle, "base_from", ""):
                print(f"base: {bundle.base_from}")
            print(bundle.diffstat or "(no changes)")
            return 0
        if args.review_cmd == "approve":
            from papaya_agent_runtime import supervision

            # The rule serve's review keeps too: the supervisor's full suite ran at this head.
            missing = supervision.full_suite_missing(args.task_id)
            if missing:
                print(f"not approved: {missing}", file=sys.stderr)
                return 1
            res = record_review(args.task_id, "approved", args.findings or "", note=args.note or "")
            print(f"approved task {args.task_id} at {res['head_sha'][:8]}")
            if res["note"]:
                print(f"note recorded with the approval: {res['note']}")
            return 0
        if args.review_cmd == "request-changes":
            res = record_review(args.task_id, "changes_requested", args.findings or "")
            print(f"requested changes on task {args.task_id} at {res['head_sha'][:8]}")
            return 0
        if args.review_cmd == "status":
            approved, reason = is_approved_at_head(args.task_id)
            print(f"task {args.task_id}: {'APPROVED' if approved else 'NOT approved'} — {reason}")
            note = approval_note(args.task_id)
            if note:
                print(f"note on that approval: {note}")
            return 0 if approved else 1
    except ReviewError as exc:
        print(f"review error: {exc}", file=sys.stderr)
        return 1
    print("no review subcommand given", file=sys.stderr)
    return 2


def _cmd_deliver(args: argparse.Namespace) -> int:
    from papaya_agent_runtime import compose
    from papaya_agent_runtime.delivery import DeliveryError, deliver, record_merged

    try:
        if args.merged:
            res = record_merged(args.task_id, args.merged)
            print(f"task {res.task_id}: {res.note}")
            line = compose.describe(res.compose)
            if line:
                print(line)
            return 0
        res = deliver(
            args.task_id,
            push=not args.no_push,
            open_pr=not args.no_pr,
            remote=args.remote,
            base=args.base,
            title=args.title,
            body_file=args.body_file,
        )
    except DeliveryError as exc:
        print(f"delivery refused: {exc}", file=sys.stderr)
        return 1
    print(f"task {res.task_id}: {res.note}")
    if res.pr_url:
        print(f"PR: {res.pr_url}")
    return 0


def _cmd_artifact(args: argparse.Namespace) -> int:
    import json as _json

    from papaya_agent_runtime.artifacts import ArtifactError, create_artifact

    try:
        sections = _json.loads(args.sections) if args.sections else []
        art = create_artifact(args.run_id, args.title, sections)
    except (ArtifactError, ValueError) as exc:
        print(f"artifact error: {exc}", file=sys.stderr)
        return 1
    print(f"artifact {art.id} written to {art.path}")
    print(f"feedback sidecar: {art.feedback_path}")
    return 0


def _cmd_feedback(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.artifacts import ArtifactError, read_feedback

    try:
        result = read_feedback(args.artifact_id)
    except ArtifactError as exc:
        print(f"feedback error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_memory(args: argparse.Namespace) -> int:
    from papaya_agent_runtime import memory
    from papaya_agent_runtime.repos import list_repos

    if args.memory_cmd == "init":
        memory.ensure_memory_layout()
        seeded = [
            memory.seed_repo_memory(
                r["name"], origin=r.get("origin"), default_branch=r.get("default_branch")
            )
            for r in list_repos()
        ]
        print(f"memory ready at {memory.memory_dir()} ({len(seeded)} repo(s) seeded)")
        return 0
    if args.memory_cmd == "path":
        print(memory.repo_dir(args.repo) if args.repo else memory.memory_dir())
        return 0
    if args.memory_cmd == "show":
        memory.ensure_memory_layout()
        if args.repo:
            notes, tasks = memory.repo_notes_path(args.repo), memory.repo_tasks_path(args.repo)
            if not notes.exists():
                print(f"no memory for repo {args.repo!r} yet", file=sys.stderr)
                return 1
            from papaya_agent_runtime import progress

            print("# ---- progress (from `ppy progress`) ----")
            print(progress.render_repo_log(args.repo))
            for path in (notes, tasks):
                if path.exists():
                    print(f"# ---- {path} ----")
                    print(path.read_text())
            return 0
        for path in (
            memory.board_path(),
            memory.preferences_path(),
            memory.relationships_path(),
            memory.improvements_path(),
        ):
            print(f"# ---- {path} ----")
            print(path.read_text())
        return 0
    print("no memory subcommand given", file=sys.stderr)
    return 2


def _cmd_hook(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.hooks import handle_hook_stdin

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    result = handle_hook_stdin(args.event, raw)
    print(json.dumps(result))
    return 0


def _cmd_handoff(args: argparse.Namespace) -> int:
    """Write the snapshot to `.ppy/memory/handoff.md` and print the short pickup prompt."""
    from papaya_agent_runtime.handoff import build_handoff

    result = build_handoff()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for warning in result["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    print(result["prompt"])
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Worker health: alive / quiet (possibly stuck) / dead, per in-flight task."""
    from datetime import timedelta

    from papaya_agent_runtime import health

    quiet_after = (
        timedelta(minutes=args.quiet_minutes)
        if args.quiet_minutes is not None
        else health.quiet_threshold()
    )
    from papaya_agent_runtime import compose

    entries = health.check(quiet_after=quiet_after)
    tools = health.claude_tool_profile()
    stacks = compose.prunable_stacks()
    dispatch = health.dispatch_snapshot()
    if args.json:
        print(
            json.dumps(
                {
                    "workers": entries,
                    "claude_tools": tools,
                    "prunable_compose_stacks": stacks,
                    "dispatch": dispatch,
                },
                indent=2,
            )
        )
        return 0
    print(health.describe_claude_tools(tools))
    pool = dispatch["pool"]
    if pool["known"]:
        print(f"worktree pool: {pool['free_slots']} of {pool['total_slots']} slot(s) free")
    else:
        print(f"worktree pool: no fixed capacity ({pool['backend']} backend)")
    print(compose.describe_prunable(stacks))
    if not entries:
        print("no workers in flight")
        return 0 if tools["ok"] else 1
    for e in entries:
        print(health.describe(e))
    troubled = [e for e in entries if e["verdict"] != "alive"]
    return 1 if troubled or not tools["ok"] else 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """The team heartbeat: one relayable line of state per tick, on a cadence."""
    from papaya_agent_runtime import watch

    try:
        return watch.run(
            interval=args.interval,
            once=args.once,
            as_json=args.json,
            exit_when_idle=args.exit_when_idle,
        )
    except KeyboardInterrupt:
        return 0


def _cmd_watermark(args: argparse.Namespace) -> int:
    """The newest processed timestamp per external record, so a sweep reads only what is new."""
    from papaya_agent_runtime import watermarks
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    if args.watermark_cmd == "set":
        try:
            stored, previous = watermarks.set_watermark(
                conn, args.key, args.timestamp, note=args.note
            )
        except watermarks.WatermarkError as exc:
            print(f"watermark set: {exc}", file=sys.stderr)
            return 1
        if previous is None:
            print(f"{args.key}: watermark {stored} (was unset)")
        elif stored < previous:
            print(
                f"{args.key}: watermark {stored} (moved back from {previous}; "
                "the next sweep re-reads)"
            )
        else:
            print(f"{args.key}: watermark {stored} (was {previous})")
        return 0
    if args.watermark_cmd in ("get", "show"):
        row = watermarks.get_watermark(conn, args.key)
        if row is None:
            print(
                f"no watermark for {args.key} — the first sweep reads everything, then "
                f"`ppy watermark set {args.key} <newest comment timestamp>`",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(dict(row), indent=2))
        else:
            print(row["watermark"])
        return 0
    if args.watermark_cmd == "clear":
        if watermarks.delete_watermark(conn, args.key):
            print(f"{args.key}: watermark cleared; the next sweep reads everything")
            return 0
        print(f"no watermark for {args.key}", file=sys.stderr)
        return 1
    rows = watermarks.list_watermarks(conn)
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    if not rows:
        print("no watermarks recorded")
        return 0
    for row in rows:
        note = f"  {row['note']}" if row["note"] else ""
        print(f"{row['watermark']}  recorded {row['recorded_at']}  {row['key']}{note}")
    return 0


def _cmd_todo(args: argparse.Namespace) -> int:
    """The manager's intent ledger: what's next and what's waiting on whom."""
    from papaya_agent_runtime import board

    try:
        if args.todo_cmd == "add":
            todo_id = board.add(
                args.text, run_id=args.run_id, task_id=args.task_id, blocked_on=args.blocked_on
            )
            print(f"todo #{todo_id} added")
            return 0
        if args.todo_cmd == "done":
            for todo_id in args.ids:
                board.done(todo_id)
            print("done: " + ", ".join(f"#{i}" for i in args.ids))
            return 0
        if args.todo_cmd == "drop":
            for todo_id in args.ids:
                board.drop(todo_id)
            print("dropped: " + ", ".join(f"#{i}" for i in args.ids))
            return 0
        if args.todo_cmd == "reopen":
            board.reopen(args.id)
            print(f"todo #{args.id} reopened")
            return 0
        if args.todo_cmd == "block":
            board.block(args.id, args.on)
            print(f"todo #{args.id} waiting on {args.on}")
            return 0
        if args.todo_cmd == "unblock":
            board.unblock(args.id)
            print(f"todo #{args.id} unblocked")
            return 0
        if args.todo_cmd == "edit":
            board.edit(args.id, args.text)
            print(f"todo #{args.id} updated")
            return 0
        if args.todo_cmd == "list":
            from papaya_agent_runtime.state import init_db, store

            conn = init_db()
            status = None if args.all else "open"
            rows = [
                board.todo_dict(r)
                for r in store.list_todos(conn, status=status, run_id=args.run_id)
            ]
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print("no open todos" if not args.all else "no todos")
                return 0
            for t in rows:
                mark = {"open": "[ ]", "done": "[x]", "dropped": "[-]"}.get(t["status"], "[?]")
                print(f"{mark} {board.todo_line(t)[2:]}")
            return 0
    except board.TodoError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("no todo subcommand given", file=sys.stderr)
    return 2


def _cmd_board(args: argparse.Namespace) -> int:
    """Render the work board from the ledger + live state (and refresh the projection)."""
    from papaya_agent_runtime import board

    text = board.write_board() if args.write else board.render_board()
    print(text, end="")
    return 0


def _cmd_progress(args: argparse.Namespace) -> int:
    """A worker's structured progress report (also usable by the manager to inspect)."""
    from papaya_agent_runtime import progress

    if args.phase is None:
        if args.history:
            entries = progress.history(args.task_id)
            if args.json:
                print(json.dumps(entries, indent=2))
                return 0
            if not entries:
                print(f"task {args.task_id}: no progress reported")
            for e in entries:
                print(f"{e['at']} · {progress.describe(e)}")
            return 0
        latest = progress.latest(args.task_id)
        if args.json:
            print(json.dumps(latest, indent=2))
            return 0
        print(f"task {args.task_id}: {progress.describe(latest)}")
        return 0
    try:
        event_id = progress.record(args.task_id, phase=args.phase, note=args.note or "")
    except progress.ProgressError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"task {args.task_id}: {args.phase} recorded")
    for note in progress.guidance_for(args.task_id, event_id):
        print(f"note from your manager: {note}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    """Run a gate where no tool timeout can cut it short, and record what it said."""
    from papaya_agent_runtime import gate

    if args.gate_cmd != "run":
        print("no gate subcommand given (try `ppy gate run`)", file=sys.stderr)
        return 2

    def out(line: str) -> None:
        print(line, flush=True)

    try:
        return gate.run_from_cli(
            task_id=args.task_id,
            repo=args.repo,
            full=args.full,
            baseline=args.baseline,
            wait_seconds=gate.WAIT_SECONDS if args.wait_seconds is None else args.wait_seconds,
            out=out,
        )
    except gate.GateError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return 2


def _cmd_receipt(args: argparse.Namespace) -> int:
    """Run one command with a task's environment and retain its output and result."""
    from papaya_agent_runtime import receipt

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        result = receipt.run(args.task_id, command)
    except receipt.ReceiptError as exc:
        print(f"receipt: {exc}", file=sys.stderr)
        return 1
    print(f"receipt: {result.output_path}")
    return result.exit_code


def _cmd_reflect(args: argparse.Namespace) -> int:
    """A worker's end-of-task reflection: its self-assessment and its assessment of the manager."""
    from papaya_agent_runtime import reflections

    if args.self_note is None and args.manager_note is None:
        entries = reflections.history(args.task_id)
        if args.json:
            print(json.dumps(entries, indent=2))
            return 0
        print(reflections.render(entries, heading=f"# task {args.task_id} — reflections"), end="")
        return 0
    try:
        reflections.record(args.task_id, self_note=args.self_note, manager_note=args.manager_note)
    except reflections.ReflectionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"task {args.task_id}: reflection recorded")
    return 0


def _cmd_need(args: argparse.Namespace) -> int:
    """A worker declares a capability it needs; policy decides or a person is asked."""
    from papaya_agent_runtime import capability_requests

    try:
        found = capability_requests.request(args.task_id, args.capability, why=args.why or "")
    except capability_requests.CapabilityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(found.public(), indent=2))
        return 0
    print(found.line())
    print(capability_requests.worker_message(found))
    return 0


def _cmd_capability(args: argparse.Namespace) -> int:
    """List workers' capability requests, or answer a pending one."""
    from papaya_agent_runtime import capability_requests
    from papaya_agent_runtime.state import init_db

    if args.capability_cmd == "list":
        conn = init_db()
        try:
            found = (
                capability_requests.all_requests(conn, task_id=args.task)
                if args.all or args.task is not None
                else capability_requests.pending(conn)
            )
        finally:
            conn.close()
        if args.json:
            print(json.dumps([r.public() for r in found], indent=2))
            return 0
        if not found:
            print("no capability requests" + ("" if args.all else " waiting on a person"))
        for item in found:
            print(item.line())
        return 0
    try:
        decided = capability_requests.decide_request(
            args.request_id,
            approve=args.capability_cmd == "approve",
            always=bool(getattr(args, "always", False)),
            reason=getattr(args, "reason", "") or "",
        )
    except capability_requests.CapabilityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(decided.line())
    where = (
        "every worker on this machine"
        if decided.scope == "install"
        else f"task {decided.task_id}'s next launch"
    )
    if decided.state == capability_requests.GRANTED:
        print(f"`{decided.pattern}` is in {where}; the worker was told and is resumed with it")
    else:
        print("the worker was told the reason")
    return 0


def _cmd_followup(args: argparse.Namespace) -> int:
    """What a stopped or done worker needs by its record, and optionally send the steer."""
    from papaya_agent_runtime import supervision
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        task = store.get_task(conn, args.task_id)
    finally:
        conn.close()
    if task is None:
        print(f"task {args.task_id} does not exist", file=sys.stderr)
        return 1
    decision = supervision.gate_followup(
        args.task_id, stopped=task["status"] == "worker_stopped", detail=""
    )
    print(f"task {args.task_id}: {decision.action} — {decision.line}")
    if decision.action != supervision.STEER:
        return 0
    if not args.send:
        print(decision.message)
        return 0
    from papaya_agent_runtime.supervisor.client import SupervisorClient

    SupervisorClient().steer_task(args.task_id, decision.message, by=store.BY_MANAGER)
    print(f"task {args.task_id}: sent back")
    return 0


def _cmd_checkin(args: argparse.Namespace) -> int:
    """Check-ins due on live workers, or record that one was done by a person."""
    from papaya_agent_runtime import supervision

    if args.task_id is None:
        due = supervision.worker_checkins()
        if not due:
            print("no worker is due a check-in")
        for item in due:
            print(item.line())
        return 0
    if not args.ok:
        print('say what you saw: --ok "..." (or steer the worker instead)', file=sys.stderr)
        return 2
    triggers = supervision.record_checkin(args.task_id, note=args.ok)
    print(
        f"task {args.task_id}: check-in recorded ({', '.join(triggers)})"
        if triggers
        else f"task {args.task_id} is not due a check-in"
    )
    return 0


def _cmd_heard(args: argparse.Namespace) -> int:
    """Say a work item change was read and needs nothing from the worker."""
    from papaya_agent_runtime import workitems

    pending = [c for c in workitems.unheard() if int(c["ticket_task_id"]) == args.task_id]
    if not pending:
        print(f"ticket task {args.task_id} has no unheard work item change")
        return 0
    workitems.mark_heard(args.task_id, args.note)
    print(f"ticket task {args.task_id}: work item change heard")
    return 0


def _cmd_assessment(args: argparse.Namespace) -> int:
    from papaya_agent_runtime import assessments
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    try:
        if args.assessment_cmd == "tick":
            cycle = assessments.ensure_due(conn, force=args.force)
            if args.json:
                print(json.dumps(cycle, indent=2))
            elif cycle is None:
                print("no assessment due")
            else:
                print(f"assessment {cycle['id']} {cycle['status']} (trigger: {cycle['trigger']})")
            return 0
        if args.assessment_cmd == "status":
            row = assessments.latest_cycle(conn)
            if row is None:
                print("no assessments yet")
                return 0
            cycle = assessments.cycle_dict(conn, int(row["id"]))
            if args.json:
                print(json.dumps(cycle, indent=2))
            else:
                print(
                    f"assessment {cycle['id']}: {cycle['status']} "
                    f"(trigger: {cycle['trigger']}, actions: {len(cycle['actions'])})"
                )
            return 0
        if args.assessment_cmd == "show":
            cycle_id = args.id
            if cycle_id is None:
                row = assessments.latest_cycle(conn)
                if row is None:
                    print("no assessments yet")
                    return 0
                cycle_id = int(row["id"])
            cycle = assessments.cycle_dict(conn, cycle_id)
            if args.json:
                print(json.dumps(cycle, indent=2))
            else:
                print(assessments.assessment_prompt(cycle))
            return 0
        if args.assessment_cmd == "complete":
            actions: list[dict] = []
            if args.action_json:
                decoded = json.loads(args.action_json)
                if not isinstance(decoded, list) or not all(isinstance(x, dict) for x in decoded):
                    raise assessments.AssessmentError(
                        "--action-json must be a JSON list of objects"
                    )
                actions = decoded
            cycle = assessments.complete(
                args.id,
                summary=args.summary,
                strengths=args.strength,
                weaknesses=args.weakness,
                actions=actions,
                conn=conn,
            )
            print(
                f"assessment {cycle['id']} recorded; align {len(cycle['actions'])} "
                "proposed actions with the user"
            )
            return 0
        if args.assessment_cmd == "align":
            cycle = assessments.align(
                args.id, decision=args.decision, notes=args.notes or "", conn=conn
            )
            print(f"assessment {cycle['id']} {cycle['status']} ({args.decision})")
            return 0
    except (assessments.AssessmentError, json.JSONDecodeError) as exc:
        print(f"assessment failed: {exc}", file=sys.stderr)
        return 1
    return 2


def _cmd_plan(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.crossrepo import DependencyError, rollout_plan

    try:
        plan = rollout_plan(args.run_id)
    except DependencyError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 1
    if plan.missing:
        for task, dep in plan.missing:
            print(f"task {task} depends on {dep}, which is not in this run", file=sys.stderr)
    if plan.cycles:
        for cycle in plan.cycles:
            print(f"dependency cycle among tasks: {cycle}", file=sys.stderr)
    print("rollout order: " + " -> ".join(str(t) for t in plan.order))
    return 0 if plan.ok else 1


def _cmd_decision(args: argparse.Namespace) -> int:
    from papaya_agent_runtime import decisions
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    if args.decision_cmd == "list":
        rows = decisions.list_decisions(conn, include_inactive=args.all)
        if not rows:
            print("no decisions recorded")
            return 0
        for d in rows:
            flags = []
            if d["superseded_by"]:
                flags.append(f"superseded->{d['superseded_by']}")
            if d["invalidated"]:
                flags.append("invalidated")
            tag = f" [{', '.join(flags)}]" if flags else ""
            print(f"{d['id']:>3} ({d['scope']}) {d['question']!r} -> {d['answer']!r}{tag}")
        return 0
    if args.decision_cmd == "invalidate":
        decisions.invalidate(conn, args.id)
        print(f"invalidated decision {args.id}")
        return 0
    if args.decision_cmd == "forget":
        decisions.forget(conn, args.id)
        print(f"forgot decision {args.id}")
        return 0
    print("no decision subcommand given", file=sys.stderr)
    return 2


def _cmd_usage(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    if store.get_run(conn, args.run_id) is None:
        print(f"run {args.run_id} not found", file=sys.stderr)
        return 1
    rows = store.usage_by_profile(conn, args.run_id)
    totals = store.usage_totals(conn, args.run_id)
    if not rows:
        print(f"run {args.run_id}: no usage recorded")
        return 0
    print(f"run {args.run_id} usage by profile:")
    for r in rows:
        model = r["model"] or "(default)"
        reasoning = r["reasoning"] or "-"
        print(
            f"  {r['provider']}/{model}@{reasoning}: "
            f"{r['input_tokens']} in / {r['output_tokens']} out ({r['calls']} call(s))"
        )
    print(f"total: {totals['input_tokens']} in / {totals['output_tokens']} out")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.reconcile()
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    findings = resp.get("reconciled", [])
    if not findings:
        print("nothing to reconcile")
    else:
        for f in findings:
            print(f"task {f['task_id']}: {f['action']}")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.config import ConfigError, load_config
    from papaya_agent_runtime.manager import ManagerLaunchError, build_launch, start

    try:
        config = load_config()
    except ConfigError:
        config = None

    provider = args.provider
    if provider is None and config is None:
        # Unconfigured and no override: pick a detected, authenticated harness so
        # the manager can launch and walk the user through setup conversationally.
        from papaya_agent_runtime.setup.discovery import discover, usable_harnesses

        usable = usable_harnesses(discover())
        if not usable:
            print(
                "no authenticated Claude/Codex harness detected; install one and "
                "sign in, then run `ppy start` (see `ppy doctor`)",
                file=sys.stderr,
            )
            return 1
        provider = usable[0]

    try:
        launch = build_launch(
            config=config,
            provider=provider,
            model=args.model,
            reasoning=args.reasoning,
            objective=args.objective,
        )
    except ManagerLaunchError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    from papaya_agent_runtime import standalone

    # Once, at the start of the session this launches; the session itself never repeats it.
    standalone.say_invitation(sys.stderr)
    if not args.dry_run and launch.provider == "claude":
        from papaya_agent_runtime import papaya
        from papaya_agent_runtime.manager.launch import repo_root

        # The session this launches acts as the connected agent, so it gets its tools.
        if papaya.status()["state"] == "connected" and not papaya.session_tools_ready(repo_root()):
            result = papaya.install_session_tools(repo_root())
            if not result["ok"]:
                print(
                    f"ppy start: Papaya tools not configured: {result['detail']}", file=sys.stderr
                )
    if args.dry_run:
        print(f"provider: {launch.provider}")
        print(f"model:    {launch.model or '(harness default)'}")
        print(f"cwd:      {launch.cwd}")
        print("PATH:     " + launch.env["PATH"].split(os.pathsep)[0] + " (prepended)")
        print("argv:     " + " ".join(launch.argv))
        return 0

    try:
        start(launch)  # replaces this process; does not return on success
    except ManagerLaunchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _cmd_status_team(args: argparse.Namespace) -> int:
    from papaya_agent_runtime import standalone, team
    from papaya_agent_runtime.paths import db_path

    snap = team.snapshot() if db_path().exists() else None
    if getattr(args, "json", False):
        print(json.dumps(snap or {}, indent=2, sort_keys=True, default=str))
        # The JSON is for a script; the invitation must not break it.
        standalone.say_invitation(sys.stderr)
        return 0
    if snap is None:
        print("team: no state yet (nothing has been picked up or dispatched on this machine)")
    else:
        for line in team.render(snap):
            print(line)
    standalone.say_invitation(sys.stdout)
    return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    from papaya_agent_runtime import team
    from papaya_agent_runtime.paths import db_path

    try:
        since = team.parse_duration(args.since)
    except ValueError as exc:
        print(f"tail: {exc}", file=sys.stderr)
        return 2
    if not db_path().exists() and not args.follow:
        return 0
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        team.tail(lambda line: print(line, flush=True), since=since, follow=args.follow)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from papaya_agent_runtime.paths import config_path, ppy_home
    from papaya_agent_runtime.repos import list_repos

    if getattr(args, "team", False) or getattr(args, "json", False):
        return _cmd_status_team(args)

    print(f"ppy home: {ppy_home()}")
    print(f"config:  {'present' if config_path().exists() else 'missing (run ppy setup)'}")
    try:
        repos = list_repos()
        print(f"repos:   {len(repos)} registered")
    except Exception:  # noqa: BLE001 - status must never crash
        print("repos:   (state not initialized)")
    try:
        from papaya_agent_runtime import board, health
        from papaya_agent_runtime.state import init_db, store

        conn = init_db()
        marks = ",".join("?" for _ in (*board.IN_FLIGHT, *board.NEEDS_ME))
        counts = dict(
            conn.execute(
                f"SELECT status, COUNT(*) FROM tasks WHERE status IN ({marks}) GROUP BY status",
                (*board.IN_FLIGHT, *board.NEEDS_ME),
            ).fetchall()
        )
        running = sum(counts.get(k, 0) for k in board.IN_FLIGHT)
        waiting = sum(counts.get(k, 0) for k in board.NEEDS_ME)
        print(f"tasks:   {running} in flight, {waiting} waiting on me")
        nxt = board.next_steps(conn)
        blocked = board.waiting(conn)
        print(f"todos:   {len(nxt)} next, {len(blocked)} waiting")
        for t in nxt[:3]:
            print(f"  next:  #{t['id']} {t['text']}")
        from papaya_agent_runtime import reconcile

        print(f"lane:    {reconcile.lane_status(conn)}")
        for advisory in health.usage_advisories(conn):
            print(health.describe_usage_advisory(advisory))
        _ = store
    except Exception:  # noqa: BLE001 - status must never crash
        pass
    try:
        from papaya_agent_runtime import assessments
        from papaya_agent_runtime.state import init_db

        row = assessments.latest_cycle(init_db())
        if row is not None and row["status"] in assessments.OPEN_STATUSES:
            print(f"review:  assessment {row['id']} {row['status']}")
    except Exception:  # noqa: BLE001 - status must never crash
        pass
    from papaya_agent_runtime import standalone

    standalone.say_invitation(sys.stdout)
    return 0


def _collect_overrides(args: argparse.Namespace) -> dict:
    keys = (
        "manager_provider",
        "manager_model",
        "manager_reasoning",
        "worker_provider",
        "worker_max_model",
        "worker_max_reasoning",
        "worker_default_model",
        "worker_default_reasoning",
        "worker_max_concurrent",
        "cost_posture",
    )
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}


def _add_profile_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manager-provider", dest="manager_provider")
    parser.add_argument("--manager-model", dest="manager_model")
    parser.add_argument("--manager-reasoning", dest="manager_reasoning")
    parser.add_argument("--worker-provider", dest="worker_provider")
    parser.add_argument("--worker-max-model", dest="worker_max_model")
    parser.add_argument("--worker-max-reasoning", dest="worker_max_reasoning")
    parser.add_argument("--worker-default-model", dest="worker_default_model")
    parser.add_argument("--worker-default-reasoning", dest="worker_default_reasoning")
    parser.add_argument("--worker-max-concurrent", dest="worker_max_concurrent", type=int)
    parser.add_argument("--cost-posture", dest="cost_posture")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ppy", description="Papaya Agent Runtime control plane")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print version").set_defaults(func=_cmd_version)

    caps = sub.add_parser(
        "capabilities",
        help="what this runtime is, for a client deciding what it can delegate here",
    )
    caps.add_argument("--json", action="store_true", help="the object a client reads")
    caps.set_defaults(func=_cmd_capabilities)

    doctor = sub.add_parser("doctor", help="environment and config diagnostics")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(func=_cmd_doctor)

    setup = sub.add_parser("setup", help="configure manager profile and worker ceiling")
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="write config from flags/defaults without prompting",
    )
    setup.add_argument(
        "--skip-tools",
        dest="skip_tools",
        action="store_true",
        help="do not provision companion tools during setup",
    )
    _add_profile_flags(setup)
    setup.set_defaults(func=_cmd_setup)

    tools = sub.add_parser("tools", help="provision and inspect companion tools")
    tsub = tools.add_subparsers(dest="tools_cmd", required=True)
    tinstall = tsub.add_parser("install", help="download/verify pinned companions into .ppy/tools")
    tinstall.add_argument("names", nargs="*", help="specific companions (default: all)")
    tinstall.add_argument("--force", action="store_true", help="reinstall even if present")
    tsub.add_parser("status", help="show companion availability")
    tools.set_defaults(func=_cmd_tools)

    config = sub.add_parser("config", help="inspect or change configuration")
    csub = config.add_subparsers(dest="config_cmd", required=True)
    csub.add_parser("show", help="print the current config")
    models = csub.add_parser("models", help="rerun discovery and change model profiles")
    _add_profile_flags(models)
    authority = csub.add_parser("authority", help="change standing authority")
    authority.add_argument(
        "--allow-merge",
        dest="allow_merge",
        action="store_true",
        default=None,
        help="grant standing merge authority (off by default)",
    )
    authority.add_argument("--no-allow-merge", dest="allow_merge", action="store_false")
    assessments_cfg = csub.add_parser("assessments", help="change review cadence")
    assessments_cfg.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    assessments_cfg.add_argument("--disabled", dest="enabled", action="store_false")
    assessments_cfg.add_argument("--completed-runs", type=int, default=None)
    assessments_cfg.add_argument("--max-days", type=int, default=None)
    assessments_cfg.add_argument("--cooldown-days", type=int, default=None)
    assessments_cfg.add_argument("--minimum-runs", type=int, default=None)
    assessments_cfg.add_argument("--failure-trigger-count", type=int, default=None)
    assessments_cfg.add_argument("--max-actions", type=int, default=None)
    claude_cfg = csub.add_parser("claude", help="the tool profile Claude workers launch with")
    claude_cfg.add_argument(
        "--allow",
        action="append",
        metavar="PATTERN",
        help="add a tool pattern to the profile (or restore a dropped one), e.g. 'Bash(go:*)'",
    )
    claude_cfg.add_argument(
        "--deny",
        action="append",
        metavar="PATTERN",
        help="take a tool pattern out of the profile",
    )
    claude_cfg.add_argument(
        "--show",
        action="store_true",
        help="list the effective tools, each marked profile, extra or dropped",
    )
    claude_cfg.add_argument("--reset", action="store_true", help="clear every addition and removal")
    claude_cfg.add_argument(
        "--lock",
        action="append",
        metavar="KEY",
        help="stop the runtime changing a key by itself (extra_tools, dropped_tools)",
    )
    claude_cfg.add_argument("--unlock", action="append", metavar="KEY")
    delivery_cfg = csub.add_parser(
        "delivery", help="what happens to a work item when its pull request merges"
    )
    delivery_cfg.add_argument(
        "--merged-status",
        default=None,
        metavar="STATUS",
        help="the status this workspace wants a merged item in (empty string: ask each time)",
    )
    caps_cfg = csub.add_parser(
        "capabilities",
        help="what this machine grants a worker that asks, without a person or never",
    )
    caps_cfg.add_argument("--auto-grant", action="append", metavar="PROGRAM", default=[])
    caps_cfg.add_argument("--never", action="append", metavar="PROGRAM", default=[])
    caps_cfg.add_argument(
        "--remove", action="append", metavar="PROGRAM", default=[], help="drop from both lists"
    )
    history_cfg = csub.add_parser(
        "history", help="every change made to the configuration, by the runtime or a person"
    )
    history_cfg.add_argument("--limit", type=int, default=None)
    history_cfg.add_argument("--json", action="store_true")

    health_cfg = csub.add_parser("health", help="change the worker quiet threshold")
    health_cfg.add_argument("--quiet-minutes", dest="quiet_minutes", type=int, default=None)
    health_cfg.add_argument("--plan-minutes", dest="plan_minutes", type=int, default=None)
    health_cfg.add_argument("--max-stale-stacks", dest="max_stale_stacks", type=int, default=None)
    config.set_defaults(func=_cmd_config)

    repo = sub.add_parser("repo", help="register and sync repositories")
    rsub = repo.add_subparsers(dest="repo_cmd", required=True)
    add = rsub.add_parser("add", help="register a repository")
    add.add_argument("url", help="repository URL or local path")
    add.add_argument(
        "--forge-url",
        dest="forge_url",
        default=None,
        help=(
            "where pull requests for this repo are opened; required when registering "
            "a local path whose own origin is not a forge"
        ),
    )
    add.add_argument("--name", default=None, help="override the derived name")
    rsub.add_parser("list", help="list registered repositories")
    rbudgets = rsub.add_parser(
        "budgets",
        help=(
            "how long each kind of wait has taken per repo, and the budget derived from it "
            "(derived, default, or override)"
        ),
    )
    rbudgets.add_argument("name", nargs="?", default=None, help="one registered repository")
    sync = rsub.add_parser(
        "sync",
        help="fetch, fast-forward the base clone's default branch, and record that commit",
    )
    sync.add_argument("name", help="registered repository name")
    sync.add_argument(
        "--clean-stray-ppy",
        dest="clean_stray_ppy",
        action="store_true",
        help="remove a stray .ppy/ directory that an ppy command created inside the base clone",
    )
    prov = rsub.add_parser(
        "provision",
        help=(
            "what a fresh worktree for this repo gets before its worker starts "
            "(no flags: show what is configured)"
        ),
    )
    prov.add_argument("name", help="registered repository name")
    prov.add_argument(
        "--command",
        default=None,
        help='shell command to run in each new worktree, e.g. "uv sync --frozen"; '
        "empty string clears it",
    )
    prov.add_argument(
        "--reuse-venv",
        dest="reuse_venv",
        default=None,
        help="repo-relative path to a virtualenv in the base clone to link into each "
        "worktree, e.g. backend/.venv; empty string clears it",
    )
    prov.add_argument("--clear", action="store_true", help="turn provisioning off for this repo")
    rshow = rsub.add_parser(
        "show",
        help="a repo's gates, each with where it came from (person, repo:<file>:<line>, "
        "observed), and its other settings",
    )
    rshow.add_argument("name", help="registered repository name")
    rset = rsub.add_parser(
        "set",
        help="per-repo settings (no flags: show what is configured)",
    )
    rset.add_argument("name", help="registered repository name")
    rset.add_argument(
        "--budget",
        action="append",
        default=None,
        metavar="KIND=SECONDS",
        help=(
            "override how long the runtime waits for KIND in this repo (gate, full_suite, "
            "worker_session, plan, silence, brief_turn, review_turn, ci); it wins over the "
            "budget derived from observations; 0 clears it; repeatable"
        ),
    )
    rset.add_argument(
        "--default-branch",
        dest="default_branch",
        default=None,
        metavar="BRANCH",
        help=(
            "pin the branch workers start from, over the forge's HEAD (registration and "
            "sync follow the forge otherwise); empty string unpins it"
        ),
    )
    rset.add_argument(
        "--migrations-glob",
        dest="migrations_glob",
        default=None,
        help=(
            "where this repo keeps database migrations, so dispatch and review can warn "
            "when two unmerged tasks add one off the same head; default "
            "**/alembic/versions/*.py, empty string restores it"
        ),
    )
    rset.add_argument(
        "--db-url-template",
        dest="db_url_template",
        default=None,
        metavar="TEMPLATE",
        help="DATABASE_URL template; placeholders: {port}, {name}, {task_id}; empty clears it",
    )
    rset.add_argument(
        "--test-db-url-template",
        dest="test_db_url_template",
        default=None,
        metavar="TEMPLATE",
        help="TEST_DATABASE_URL template; placeholders: {port}, {name}, {task_id}; empty clears it",
    )
    rset.add_argument(
        "--source-line-ceiling",
        dest="source_line_ceiling",
        default=None,
        metavar="LINES",
        help="maximum source-file line count enforced by the repo harness; 0 clears it",
    )
    rset.add_argument(
        "--needs-elevated-localhost",
        dest="needs_elevated_localhost",
        action="store_true",
        default=None,
        help="record that localhost and Git object operations may need the elevated path",
    )
    # The environment block every worker dispatched to this repo is handed
    # (issue #60). Each is optional; `no` or an empty string clears it.
    rset.add_argument(
        "--compose-stack",
        dest="compose_stack",
        default=None,
        metavar="YES|NO|FILE",
        help=(
            "this repo runs a per-task database stack with docker compose: `yes` for the "
            "repo's default compose file, or the compose file's repo-relative path; dispatch "
            "then assigns compose_project=task_<n> and a port and puts the override recipe "
            "in the worker's brief; `no` clears it"
        ),
    )
    rset.add_argument(
        "--db-port-base",
        dest="db_port_base",
        default=None,
        metavar="PORT",
        help=(
            "the base a task's database host port is derived from (base + task id), "
            "e.g. 54000; 0 clears it"
        ),
    )
    rset.add_argument(
        "--db-port-variable",
        dest="db_port_variable",
        default=None,
        metavar="NAME",
        help=(
            "the variable name the port travels under in make overrides and the root .env "
            "(default DB_PORT; e.g. PAPAYA_DB_PORT); empty string restores the default"
        ),
    )
    rset.add_argument(
        "--push-hook-runs-full-suite",
        dest="push_hook_runs_full_suite",
        default=None,
        metavar="YES|NO",
        help=(
            "this repo's pre-push hook runs the full suite: workers are told to stop at the "
            "code-level gates, commit, and report the head SHA in their done note, and the "
            "harness pushes the lease branch on their behalf"
        ),
    )
    rset.add_argument(
        "--local-gate",
        dest="local_gate",
        default=None,
        metavar="COMMAND",
        help=(
            "the scoped suite a worker runs before reporting done, e.g. "
            '"make test-backend"; the full suite is CI\'s; empty string clears it'
        ),
    )
    rset.add_argument(
        "--full-suite-owner",
        dest="full_suite_owner",
        default=None,
        metavar="WHO",
        help="who runs the full suite (default ci); empty string restores the default",
    )
    rset.add_argument(
        "--full-suite-command",
        dest="full_suite_command",
        default=None,
        metavar="COMMAND",
        help='the command `ppy gate run --full` runs, e.g. "make verify"; empty string clears it',
    )
    rset.add_argument(
        "--auto-merge",
        dest="auto_merge",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "merge this repo's delivered pull requests once they have sat green, mergeable and "
            "with no changes requested for delivery.merge_after_hours (off by default; "
            "--no-auto-merge turns it off)"
        ),
    )
    rset.add_argument(
        "--merge-method",
        dest="merge_method",
        default=None,
        choices=["squash", "merge", "rebase", ""],
        help="how --auto-merge merges (default squash); empty string restores the default",
    )
    rset.add_argument(
        "--evidence-dir",
        dest="evidence_dir",
        default=None,
        metavar="DIR",
        help=(
            "worktree-relative directory workers write receipts to (default .ppy-evidence; "
            "excluded from version control, listed by `ppy review show`); empty string "
            "restores it"
        ),
    )
    discover = rsub.add_parser(
        "discover",
        help="repositories on the forge that are not registered yet, newest activity first",
    )
    discover.add_argument(
        "--owner",
        default=None,
        help="a single account or organization; default is you plus every org you belong to",
    )
    discover.add_argument(
        "--limit", type=int, default=50, help="how many repositories to read per owner"
    )
    discover.add_argument(
        "--top", type=int, default=15, help="how many candidates to print (default 15)"
    )
    discover.add_argument(
        "--include-forks",
        dest="include_forks",
        action="store_true",
        help="offer forks too; by default they are skipped because work belongs upstream",
    )
    discover.add_argument("--json", action="store_true", help="machine-readable output")
    onboard_cmd = rsub.add_parser(
        "onboard",
        help="read a registered repo's build, tests, CI gate and conventions into its notes",
    )
    onboard_cmd.add_argument("name", help="registered repository name")
    onboard_cmd.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="print what would be recorded without writing it",
    )
    onboard_cmd.add_argument(
        "--local-gate",
        dest="local_gate",
        default=None,
        metavar="COMMAND",
        help="record this as the scoped local gate instead of the one derived from the repo",
    )
    onboard_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    ensure_cmd = rsub.add_parser(
        "ensure",
        help="make a repo ready to work in, registering and onboarding it if it is not yet",
    )
    ensure_cmd.add_argument(
        "repo", help="a registered name, an owner/name slug, or a repository URL"
    )
    ensure_cmd.add_argument(
        "--allow-outside",
        dest="allow_outside",
        action="store_true",
        help=(
            "register even when the repo is outside your account and organisations; "
            "for an explicit human yes, never for work that merely named it"
        ),
    )
    ensure_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    locate_cmd = rsub.add_parser(
        "locate",
        help=(
            "which registered clones contain these strings (literal, case-insensitive, "
            "every term in the same file), with hits per repository and the top files"
        ),
    )
    locate_cmd.add_argument(
        "terms", nargs="+", help='distinctive strings from the ticket, e.g. "hover card"'
    )
    locate_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    repo.set_defaults(func=_cmd_repo)

    ready = sub.add_parser(
        "readiness",
        help="can this runtime take work? one verdict, with who has to fix each gap",
    )
    ready.add_argument("--json", action="store_true", help="machine-readable verdict")
    ready.add_argument(
        "--report",
        action="store_true",
        help="print the message the connection owner should be sent, and nothing else",
    )
    ready.add_argument(
        "--agent",
        default=None,
        help="how to name this agent in the report, e.g. @engineering_agent",
    )
    ready.add_argument(
        "--where", default=None, help="where this runtime lives, for a report read away from it"
    )
    ready.add_argument(
        "--mark-reported",
        dest="mark_reported",
        action="store_true",
        help="record that the owner has been told, so an unchanged verdict stays quiet",
    )
    ready.add_argument(
        "--forget",
        action="store_true",
        help="clear what has been reported, so the next check speaks again",
    )
    ready.set_defaults(func=_cmd_readiness)

    blockers_cmd = sub.add_parser(
        "blockers",
        help="what this machine needs from a person, with the exact commands (exit 1 if any)",
    )
    blockers_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    blockers_cmd.set_defaults(func=_cmd_blockers)

    papaya_cmd = sub.add_parser(
        "papaya", help="the Papaya connection: who this runtime is, and establishing it"
    )
    psub = papaya_cmd.add_subparsers(dest="papaya_cmd", required=True)
    pstatus = psub.add_parser("status", help="which Papaya agent this machine is connected as")
    pstatus.add_argument("--json", action="store_true", help="machine-readable output")
    pconnect = psub.add_parser(
        "connect", help="sign in and pin this machine to a Papaya agent (opens a browser)"
    )
    pconnect.add_argument(
        "--harness",
        default="claude",
        choices=("claude", "codex", "cursor"),
        help="which harness to install the Papaya plugin and hooks for",
    )
    pcontext = psub.add_parser(
        "context", help="the connected agent's persona, rules and memories, as JSON"
    )
    pcontext.add_argument(
        "--refresh", action="store_true", help="re-fetch instead of reading the cache"
    )
    ptools = psub.add_parser(
        "tools",
        help="give Claude Code sessions in the runtime directory this agent's Papaya tools "
        "(the MCP server `ppy serve` turns use)",
    )
    ptools.add_argument("--check", action="store_true", help="only say whether they are set up")
    ptools.add_argument("--json", action="store_true", help="machine-readable output")
    papaya_cmd.set_defaults(func=_cmd_papaya)

    track = sub.add_parser(
        "track",
        help="record which tracker record a task belongs to (Papaya, Linear, Notion, anything)",
    )
    track.add_argument("task", type=int, help="task id")
    track.add_argument(
        "--record",
        default=None,
        help="the record's id as its tracker spells it, e.g. PAP-214 or ENG-1183",
    )
    track.add_argument(
        "--provider",
        default="papaya",
        help=(
            "which tracker holds it — papaya, linear, notion, jira, or any name a "
            "workspace uses. Defaults to papaya only when nobody has said otherwise"
        ),
    )
    track.add_argument("--url", default=None, help="the record's URL, for anyone reading the PR")
    track.add_argument("--title", default=None, help="the record's title, so copy can name it")
    track.add_argument(
        "--show", action="store_true", help="print what this task is tracked as, and change nothing"
    )
    track.set_defaults(func=_cmd_track)

    serve_cmd = sub.add_parser(
        "serve",
        help=(
            "run the Papaya manager: this runtime's supervisor and the Papaya client's "
            "event loop, in one process, until told to stop"
        ),
    )
    serve_cmd.add_argument(
        "listen_args",
        nargs=argparse.REMAINDER,
        help=(
            "flags the Papaya client passes across its exec: --supervised, --harness, "
            "--approval-timeout, --working-directory. Unknown `listen` flags are ignored "
            "with a warning. Run `ppy serve --help` for the full list."
        ),
    )
    serve_cmd.set_defaults(func=_cmd_serve)

    sweep_cmd = sub.add_parser(
        "sweep",
        help=(
            "ask the running `ppy serve` to look for work assigned to this agent that "
            "nothing has picked up, now, and print what it found"
        ),
    )
    sweep_cmd.add_argument(
        "--include-declined",
        "--include-kept",
        dest="include_declined",
        action="store_true",
        help=(
            "also offer tickets this runtime declined earlier, and ones Papaya recently "
            "kept elsewhere, that nobody has changed since; the timed sweep leaves those "
            "alone. The two spellings are one flag"
        ),
    )
    sweep_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    sweep_cmd.set_defaults(func=_cmd_sweep)

    deficiency = sub.add_parser(
        "deficiency", help="structural deficiencies the runtime found in itself"
    )
    dsub = deficiency.add_subparsers(dest="deficiency_cmd", required=True)
    dlist = dsub.add_parser(
        "list", help="the ledger, with the GitHub issue `ppy serve` opened for each"
    )
    dlist.add_argument(
        "--all", action="store_true", help="include deficiencies still below their threshold"
    )
    dlist.add_argument("--json", action="store_true", help="machine-readable output")
    deficiency.set_defaults(func=_cmd_deficiency)

    supervisor = sub.add_parser("supervisor", help="run and control the supervisor")
    ssub = supervisor.add_subparsers(dest="supervisor_cmd", required=True)
    ssub.add_parser("serve", help="run the supervisor (blocking)")
    ssub.add_parser("status", help="ping a running supervisor")
    ssub.add_parser("stop", help="ask a running supervisor to shut down")
    supervisor.set_defaults(func=_cmd_supervisor)

    dispatch = sub.add_parser("dispatch", help="dispatch a task to a registered repo")
    dispatch.add_argument("--repo", required=True, help="registered repository name")
    dispatch.add_argument(
        "--title",
        default=None,
        help=(
            "the objective, in one line; optional when --brief is given, which takes it "
            "from the brief's first Markdown heading. An explicit --title always wins"
        ),
    )
    dispatch.add_argument("--instructions", default=None, help="task instructions")
    dispatch.add_argument(
        "--ends-at",
        choices=["review", "done"],
        default="done",
        help="worker terminal progress phase (default: done)",
    )
    dispatch.add_argument(
        "--brief",
        default=None,
        help=(
            "read the task instructions from this file; refused if missing or empty, "
            "and archived under .ppy/briefs/<repo>/task-<id>.md after dispatch"
        ),
    )
    dispatch.add_argument(
        "--strict",
        action="store_true",
        help=(
            "refuse to dispatch when the brief lint finds anything (by default findings "
            "are printed as warnings and the dispatch goes ahead)"
        ),
    )
    dispatch.add_argument(
        "--provider",
        default=None,
        choices=["fake", "claude", "codex"],
        help="worker provider (default: the configured one); `fake` only when asked for by name",
    )
    dispatch.add_argument("--model", default=None, help="worker model (clamped to ceiling)")
    dispatch.add_argument("--reasoning", default=None, help="worker reasoning (clamped to ceiling)")
    dispatch.add_argument(
        "--run-id", dest="run_id", type=int, default=None, help="attach to an existing run"
    )
    dispatch.add_argument(
        "--stack-on",
        dest="stack_on",
        type=int,
        default=None,
        help=(
            "build on this task: the worker starts from that task's lease branch and "
            "the stack parent is recorded, so `ppy stack` and `ppy deliver` follow the "
            "chain without being told a branch name. The parent need not have pushed "
            "yet: a branch not on the forge is taken from the base clone or the "
            "parent's lease worktree"
        ),
    )
    dispatch.add_argument(
        "--base",
        default=None,
        help=(
            "start the worker from <branch> instead of the repo base (stacked PRs): "
            "the forge's copy when it has one, else the local lease branch; recorded "
            "on the task so `ppy deliver` opens the PR against it"
        ),
    )
    dispatch.set_defaults(func=_cmd_dispatch)

    brief = sub.add_parser("brief", help="check a task brief before dispatching it")
    bsub = brief.add_subparsers(dest="brief_cmd", required=True)
    blint = bsub.add_parser(
        "lint",
        help=(
            "the shape a worker can be right about: Symptom before Hypotheses, a probe per "
            "hypothesis, an Expected discrepancies row per hypothesis, scope rules that do "
            "not contradict the required cases, and no evidence under /tmp. Exit 1 on findings"
        ),
    )
    blint.add_argument("file", help="the brief (Markdown)")
    blint.add_argument(
        "--ends-at",
        choices=["review", "done"],
        default="done",
        help="terminal phase to check the brief against (default: done)",
    )
    brief.set_defaults(func=_cmd_brief)

    worktree = sub.add_parser("worktree", help="inspect and reclaim leased and orphaned worktrees")
    wtsub = worktree.add_subparsers(dest="worktree_cmd", required=True)
    wtlist = wtsub.add_parser(
        "list",
        help=(
            "slot, task, status, branch, dirty/clean, size — leased slots plus the "
            "pool directories on disk no active lease owns (shown as orphaned)"
        ),
    )
    wtlist.add_argument("--repo", default=None, help="only this registered repo")
    wtlist.add_argument("--json", action="store_true", help="machine-readable output")
    wtprune = wtsub.add_parser(
        "prune",
        help=(
            "remove the worktrees of finished tasks, and the orphaned slots, that "
            "hold nothing unique"
        ),
    )
    wtprune.add_argument("--repo", default=None, help="only this registered repo")
    wtprune.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="report what would go without touching anything",
    )
    worktree.set_defaults(func=_cmd_worktree)

    wait = sub.add_parser(
        "wait",
        help="BLOCKING long-poll until a run is actionable (scripts/tests; "
        "use --timeout 0 for a non-blocking drain). Prefer `ppy run` in a live turn.",
    )
    wait.add_argument("run_id", type=int)
    wait.add_argument("--timeout", type=float, default=30.0)
    wait.add_argument(
        "--after-seq",
        dest="after_seq",
        type=int,
        default=0,
        help="only report actionable events with seq greater than this (a cursor)",
    )
    wait.set_defaults(func=_cmd_wait)

    run = sub.add_parser(
        "run", help="non-blocking snapshot of a run (task states, actionable events, usage)"
    )
    run.add_argument("run_id", type=int)
    run.add_argument("--json", action="store_true", help="machine-readable output")
    run.set_defaults(func=_cmd_run)

    task = sub.add_parser("task", help="inspect a task, or close/repair its bookkeeping")
    tksub = task.add_subparsers(dest="task_cmd", required=True)
    tkshow = tksub.add_parser("show", help="non-blocking snapshot of a single task")
    tkshow.add_argument("task_id", type=int)
    tkshow.add_argument("--json", action="store_true", help="machine-readable output")
    tkclose = tksub.add_parser(
        "close", help="end a task that will not be delivered, and give its slot back"
    )
    tkclose.add_argument("task_id", type=int)
    tkclose.add_argument("--reason", required=True, help="why this task is over")
    tkpush = tksub.add_parser(
        "push",
        help="push a task's lease worktree onto its own branch (what the harness does itself)",
    )
    tkpush.add_argument("task_id", type=int)
    tkset = tksub.add_parser("set-status", help="repair a task's status, with a note")
    tkset.add_argument("task_id", type=int)
    tkset.add_argument("status", choices=list(TASK_STATUSES))
    tkset.add_argument("--note", default=None, help="why the status was corrected")
    tkenv = tksub.add_parser(
        "env",
        help=(
            "small facts attached to a task — set `compose_project=<name>` and the "
            "task's compose stack is torn down when the task ends"
        ),
    )
    tkenvsub = tkenv.add_subparsers(dest="task_env_cmd", required=True)
    tkenvset = tkenvsub.add_parser("set", help="record key=value on a task")
    tkenvset.add_argument("task_id", type=int)
    tkenvset.add_argument(
        "assignments",
        nargs="+",
        metavar="KEY=VALUE",
        help="e.g. compose_project=task_12",
    )
    tkenvshow = tkenvsub.add_parser("show", help="everything recorded on a task")
    tkenvshow.add_argument("task_id", type=int)
    tkenvshow.add_argument("--json", action="store_true", help="machine-readable output")
    task.set_defaults(func=_cmd_task)

    stack = sub.add_parser(
        "stack",
        help="render, merge, or rebuild a native pull-request stack",
    )
    stack.add_argument(
        "command_or_id",
        help="a task/run id to view, or `merge` / `rebuild`",
    )
    stack.add_argument("id", nargs="?", type=int, help="task id for merge/rebuild")
    stack.add_argument(
        "--all",
        action="store_true",
        help="with merge, continue upward until the stack is done or a required check is red",
    )
    stack.add_argument("--json", action="store_true", help="machine-readable output")
    stack.set_defaults(func=_cmd_stack)

    lease = sub.add_parser("lease", help="worktree lease bookkeeping")
    lsub = lease.add_subparsers(dest="lease_cmd", required=True)
    lrelease = lsub.add_parser("release", help="release a stuck lease and free the slot")
    lrelease.add_argument("task_id", type=int)
    lrelease.add_argument("--reason", default="released by hand", help="why it was released")
    lrelease.add_argument(
        "--remove-branch",
        dest="remove_branch",
        action="store_true",
        help="also delete the lease branch (only when its commits are safe elsewhere)",
    )
    lease.set_defaults(func=_cmd_lease)

    resume = sub.add_parser("resume", help="resume a task's provider session")
    resume.add_argument("task_id", type=int)
    resume.add_argument("--message", default=None, help="answer/steer message")
    resume.add_argument(
        "--ends-at",
        choices=["review", "done"],
        default=None,
        help="override and persist the task's terminal phase (default: stored value)",
    )
    resume.set_defaults(func=_cmd_resume)

    steer = sub.add_parser("steer", help="steer a task (checkpoint or capability-gated interrupt)")
    steer.add_argument("task_id", type=int)
    steer.add_argument("--message", required=True, help="steer message")
    steer.add_argument(
        "--replace",
        action="store_true",
        help=(
            "when queued for a checkpoint, this message supersedes every message queued "
            "before it (default: queued messages are all delivered together, in order)"
        ),
    )
    steer.set_defaults(func=_cmd_steer)

    stop = sub.add_parser(
        "stop",
        help=(
            "stop a worker's current turn and resume it with this message alone "
            "(a steer that supersedes everything queued)"
        ),
    )
    stop.add_argument("task_id", type=int)
    stop.add_argument("--message", required=True, help="what the worker does instead")
    stop.set_defaults(func=_cmd_steer, stop=True, replace=True)

    answer = sub.add_parser("answer", help="answer a blocked task and record a durable decision")
    answer.add_argument("task_id", type=int)
    answer.add_argument("--answer", required=True, help="the answer to the pending question")
    answer.add_argument(
        "--scope",
        default="run",
        choices=["task", "run", "global"],
        help="how widely the decision applies (default: run)",
    )
    answer.add_argument("--rationale", default=None, help="why this answer was chosen")
    answer.set_defaults(func=_cmd_answer)

    review = sub.add_parser("review", help="review a task's work (exact-HEAD gate)")
    rvsub = review.add_subparsers(dest="review_cmd", required=True)
    for name in ("show", "approve", "request-changes", "status"):
        p = rvsub.add_parser(name)
        p.add_argument("task_id", type=int)
        if name in ("approve", "request-changes"):
            p.add_argument("--findings", default=None, help="review notes")
        if name == "approve":
            p.add_argument(
                "--note",
                default=None,
                help=(
                    "what you checked and accepted, in your own words; stored against the "
                    "commit you approved, printed by `ppy review status|show`, and quoted "
                    "in the pull request `ppy deliver` opens"
                ),
            )
    review.set_defaults(func=_cmd_review)

    deliver = sub.add_parser("deliver", help="push and open a PR (requires approval at head)")
    deliver.add_argument("task_id", type=int)
    deliver.add_argument("--no-push", dest="no_push", action="store_true")
    deliver.add_argument("--no-pr", dest="no_pr", action="store_true")
    deliver.add_argument(
        "--remote",
        default=None,
        help="push target; defaults to the repo's registered forge remote",
    )
    deliver.add_argument("--base", default=None, help="PR base branch")
    deliver.add_argument(
        "--title",
        default=None,
        help="pull request title; defaults to the objective the task was dispatched with",
    )
    deliver.add_argument(
        "--body-file",
        dest="body_file",
        default=None,
        metavar="FILE",
        help=(
            "use this file as the pull request body verbatim, instead of the body "
            "composed from the brief, the worker's reports, and the approval note"
        ),
    )
    deliver.add_argument(
        "--merged",
        default=None,
        metavar="SHA",
        help="record delivery against a commit already merged upstream; pushes nothing",
    )
    deliver.set_defaults(func=_cmd_deliver)

    artifact = sub.add_parser("artifact", help="write a Lavish HTML review artifact")
    artifact.add_argument("run_id", type=int)
    artifact.add_argument("--title", required=True)
    artifact.add_argument("--sections", default=None, help="JSON list of {heading,body,options}")
    artifact.set_defaults(func=_cmd_artifact)

    feedback = sub.add_parser("feedback", help="read a Lavish artifact's feedback sidecar")
    feedback.add_argument("artifact_id", type=int)
    feedback.set_defaults(func=_cmd_feedback)

    memory = sub.add_parser(
        "memory", help="durable memory under .ppy/memory (per-instance + per-repo)"
    )
    msub = memory.add_subparsers(dest="memory_cmd", required=True)
    msub.add_parser("init", help="scaffold instance memory + seed a dir per registered repo")
    mpath = msub.add_parser("path", help="print the memory dir, or a repo's memory dir")
    mpath.add_argument("--repo", default=None, help="print this repo's memory directory")
    mshow = msub.add_parser(
        "show", help="print instance preferences+relationships, or a repo's memory"
    )
    mshow.add_argument("--repo", default=None, help="show this repo's notes + tasks instead")
    memory.set_defaults(func=_cmd_memory)

    todo = sub.add_parser("todo", help="the manager's intent ledger: next steps and waits")
    tsub = todo.add_subparsers(dest="todo_cmd", required=True)
    tadd = tsub.add_parser("add", help="record a next step")
    tadd.add_argument("text")
    tadd.add_argument("--run", dest="run_id", type=int, default=None)
    tadd.add_argument("--task", dest="task_id", type=int, default=None)
    tadd.add_argument(
        "--blocked-on",
        dest="blocked_on",
        default=None,
        help="user[:what] | review | task:<id> | access[:what]",
    )
    tdone = tsub.add_parser("done", help="mark todo(s) done")
    tdone.add_argument("ids", type=int, nargs="+")
    tdrop = tsub.add_parser("drop", help="drop todo(s) without doing them")
    tdrop.add_argument("ids", type=int, nargs="+")
    treopen = tsub.add_parser("reopen", help="reopen a done/dropped todo")
    treopen.add_argument("id", type=int)
    tblock = tsub.add_parser("block", help="mark a todo as waiting on someone/something")
    tblock.add_argument("id", type=int)
    tblock.add_argument("--on", required=True, help="user[:what] | review | task:<id> | access")
    tunblock = tsub.add_parser("unblock", help="clear a todo's wait")
    tunblock.add_argument("id", type=int)
    tedit = tsub.add_parser("edit", help="rewrite a todo's text")
    tedit.add_argument("id", type=int)
    tedit.add_argument("text")
    tlist = tsub.add_parser("list", help="list open todos (or all)")
    tlist.add_argument("--all", action="store_true")
    tlist.add_argument("--run", dest="run_id", type=int, default=None)
    tlist.add_argument("--json", action="store_true")
    todo.set_defaults(func=_cmd_todo)

    board_cmd = sub.add_parser("board", help="render the work board from ledger + live state")
    board_cmd.add_argument("--write", action="store_true", help="also refresh .ppy/memory/tasks.md")
    board_cmd.set_defaults(func=_cmd_board)

    prog = sub.add_parser(
        "progress", help="worker progress report (record with --phase; inspect without)"
    )
    prog.add_argument("task_id", type=int)
    prog.add_argument("--phase", choices=["plan", "implement", "test", "review", "blocked", "done"])
    prog.add_argument("--note", default="")
    prog.add_argument("--history", action="store_true", help="show every report, oldest last")
    prog.add_argument("--json", action="store_true")
    prog.set_defaults(func=_cmd_progress)

    receipt = sub.add_parser("receipt", help="run a command and retain task evidence")
    receipt.add_argument("task_id", type=int)
    receipt.add_argument("command", nargs=argparse.REMAINDER, help="command to run after --")
    receipt.set_defaults(func=_cmd_receipt)

    gate_parser = sub.add_parser(
        "gate",
        help="run a repository's gate under the supervisor, outside any tool call's timeout",
    )
    gate_sub = gate_parser.add_subparsers(dest="gate_cmd")
    gate_run = gate_sub.add_parser(
        "run",
        help=(
            "run the local gate (or --full suite) as the supervisor's own process, print a "
            "progress line every minute, and record the result against the head commit; "
            "exits 0 green, 1 red, 75 still running (run it again to keep waiting)"
        ),
    )
    gate_run.add_argument(
        "repo",
        nargs="?",
        default=None,
        help="the repository; without --task the gate runs in its base clone",
    )
    gate_run.add_argument(
        "--task", dest="task_id", type=int, default=None, help="run in this task's worktree"
    )
    gate_run.add_argument(
        "--full", action="store_true", help="run the repository's full suite, not its local gate"
    )
    gate_run.add_argument(
        "--baseline",
        default=None,
        metavar="SHA",
        help=(
            "gate this base commit instead, in a scratch worktree and a database of its own "
            "(the repository comes from --task or the positional name)"
        ),
    )
    gate_run.add_argument(
        "--wait",
        dest="wait_seconds",
        type=float,
        default=None,
        metavar="SECONDS",
        help="how long this call waits before answering 'still running' (default 540)",
    )
    gate_parser.set_defaults(func=_cmd_gate)

    refl = sub.add_parser(
        "reflect",
        help="worker reflection: --self (own assessment) and/or --manager (assessment of the "
        "manager); without either, show the task's reflections",
    )
    refl.add_argument("task_id", type=int)
    refl.add_argument(
        "--self", dest="self_note", default=None, help="what you learned, what went well or badly"
    )
    refl.add_argument(
        "--manager",
        dest="manager_note",
        default=None,
        help="how the brief, scope, steering, and review served you",
    )
    refl.add_argument("--json", action="store_true")
    refl.set_defaults(func=_cmd_reflect)

    followup = sub.add_parser(
        "followup",
        help="what a stopped or done worker needs by its recorded gate and worktree "
        "(the decision `ppy serve` makes too); --send steers it",
    )
    followup.add_argument("task_id", type=int)
    followup.add_argument("--send", action="store_true", help="send the steer it names")
    followup.set_defaults(func=_cmd_followup)

    heard = sub.add_parser(
        "heard",
        help="a work item change was read and needs nothing from its worker (a steer also counts)",
    )
    heard.add_argument("task_id", type=int, help="the ticket task")
    heard.add_argument("--note", required=True, help="what you read and why nothing is needed")
    heard.set_defaults(func=_cmd_heard)

    checkin = sub.add_parser(
        "checkin",
        help="live workers due a check-in (the decision `ppy serve` makes too), or record one",
    )
    checkin.add_argument("task_id", type=int, nargs="?")
    checkin.add_argument("--ok", default="", help="what you saw, when no steer was needed")
    checkin.set_defaults(func=_cmd_checkin)

    need = sub.add_parser(
        "need",
        help="a worker declares a program it needs that its tools may not allow; the runtime "
        "grants it by policy or asks a person",
    )
    need.add_argument("task_id", type=int)
    need.add_argument(
        "--capability",
        required=True,
        metavar="PROGRAM",
        help="one program, e.g. xcodegen (or Bash(xcodegen:*))",
    )
    need.add_argument("--why", default="", help="what the task needs it for, in one line")
    need.add_argument("--json", action="store_true")
    need.set_defaults(func=_cmd_need)

    capability = sub.add_parser(
        "capability", help="workers' capability requests: list them, approve or deny one"
    )
    capsub = capability.add_subparsers(dest="capability_cmd", required=True)
    cap_list = capsub.add_parser("list", help="requests waiting on a person (--all for every one)")
    cap_list.add_argument("--all", action="store_true")
    cap_list.add_argument("--task", type=int, default=None)
    cap_list.add_argument("--json", action="store_true")
    cap_approve = capsub.add_parser("approve", help="grant a pending request")
    cap_approve.add_argument("request_id", type=int)
    cap_approve.add_argument(
        "--always",
        action="store_true",
        help="grant it to every worker on this machine, not only this task",
    )
    cap_deny = capsub.add_parser("deny", help="refuse a pending request, with the reason")
    cap_deny.add_argument("request_id", type=int)
    cap_deny.add_argument("--reason", required=True)
    capability.set_defaults(func=_cmd_capability)

    handoff = sub.add_parser(
        "handoff",
        help="write the snapshot to .ppy/memory/handoff.md and print a short pickup prompt",
    )
    handoff.add_argument("--json", action="store_true", help="machine-readable output")
    handoff.set_defaults(func=_cmd_handoff)

    watch_cmd = sub.add_parser(
        "watch",
        help=(
            "team heartbeat: print one line of state (in-flight workers, tasks waiting "
            "on you, the pull requests delivered work sits in with their CI verdict, "
            "new events) now and every --interval seconds; relay each tick"
        ),
    )
    watch_cmd.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help="seconds between ticks (default 300 — the 5-minute check-in)",
    )
    watch_cmd.add_argument("--once", action="store_true", help="print one tick and exit")
    watch_cmd.add_argument("--json", action="store_true", help="machine-readable ticks")
    watch_cmd.add_argument(
        "--exit-when-idle",
        dest="exit_when_idle",
        action="store_true",
        help=(
            "for scripts: exit on the second idle tick instead of going quiet and "
            "waiting for the team to have news again"
        ),
    )
    watch_cmd.set_defaults(func=_cmd_watch)

    watermark = sub.add_parser(
        "watermark",
        help=(
            "the newest comment timestamp you have processed per external record (ticket "
            "URL or id), so a sweep asks only for what is newer"
        ),
    )
    wmsub = watermark.add_subparsers(dest="watermark_cmd", required=True)
    wmset = wmsub.add_parser("set", help="record the newest processed timestamp for a record")
    wmset.add_argument("key", help="the record: a ticket URL or id, free-form")
    wmset.add_argument("timestamp", help="ISO-8601, e.g. 2026-09-06T14:03:00Z; stored as UTC")
    wmset.add_argument("--note", default=None, help="what the sweep did with it, optional")
    wmget = wmsub.add_parser(
        "get", aliases=["show"], help="print the watermark (exit 1 and say so when unset)"
    )
    wmget.add_argument("key")
    wmget.add_argument("--json", action="store_true", help="the whole row")
    wmclear = wmsub.add_parser("clear", help="forget a record's watermark; the next sweep re-reads")
    wmclear.add_argument("key")
    wmlist = wmsub.add_parser("list", help="every watermark, by key")
    wmlist.add_argument("--json", action="store_true", help="machine-readable output")
    watermark.set_defaults(func=_cmd_watermark)

    health_cmd = sub.add_parser("health", help="are in-flight workers alive and talking?")
    health_cmd.add_argument(
        "--quiet-minutes",
        dest="quiet_minutes",
        type=int,
        default=None,
        help="override the configured silence threshold for this check",
    )
    health_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    health_cmd.set_defaults(func=_cmd_health)

    hook = sub.add_parser("hook", help="ingest a provider lifecycle hook (payload on stdin)")
    hook.add_argument("event", help="lifecycle event name (e.g. stop, session-start)")
    hook.set_defaults(func=_cmd_hook)

    assessment = sub.add_parser(
        "assessment", help="inspect and record proactive manager performance reviews"
    )
    asub = assessment.add_subparsers(dest="assessment_cmd", required=True)
    atick = asub.add_parser("tick", help="prepare a cycle if policy says one is due")
    atick.add_argument("--force", action="store_true", help="prepare a cycle regardless of cadence")
    atick.add_argument("--json", action="store_true")
    astatus = asub.add_parser("status", help="show the latest cycle")
    astatus.add_argument("--json", action="store_true")
    ashow = asub.add_parser("show", help="show the assessment prompt or state")
    ashow.add_argument("id", type=int, nargs="?", default=None)
    ashow.add_argument("--json", action="store_true")
    acomplete = asub.add_parser("complete", help="record findings and proposed experiments")
    acomplete.add_argument("id", type=int)
    acomplete.add_argument("--summary", required=True)
    acomplete.add_argument("--strength", action="append", default=[])
    acomplete.add_argument("--weakness", action="append", default=[])
    acomplete.add_argument(
        "--action-json",
        default=None,
        help=(
            "JSON actions with description/category/observation/likely_cause/"
            "baseline/target/measurement"
        ),
    )
    aalign = asub.add_parser("align", help="record the user's decision on the plan")
    aalign.add_argument("id", type=int)
    aalign.add_argument("--decision", choices=["approved", "revised", "dismissed"], required=True)
    aalign.add_argument("--notes", default=None)
    assessment.set_defaults(func=_cmd_assessment)

    plan = sub.add_parser("plan", help="compute cross-repo rollout order for a run")
    plan.add_argument("run_id", type=int)
    plan.set_defaults(func=_cmd_plan)

    decision = sub.add_parser("decision", help="inspect and manage durable decisions")
    dsub = decision.add_subparsers(dest="decision_cmd", required=True)
    dlist = dsub.add_parser("list", help="list decisions")
    dlist.add_argument("--all", action="store_true", help="include superseded/invalidated")
    dinval = dsub.add_parser("invalidate", help="mark a decision no longer valid")
    dinval.add_argument("id", type=int)
    dforget = dsub.add_parser("forget", help="hard-delete a decision")
    dforget.add_argument("id", type=int)
    decision.set_defaults(func=_cmd_decision)

    usage = sub.add_parser("usage", help="report model usage for a run")
    usage.add_argument("run_id", type=int)
    usage.set_defaults(func=_cmd_usage)

    reconcile = sub.add_parser("reconcile", help="recover half-alive runners")
    reconcile.set_defaults(func=_cmd_reconcile)

    status = sub.add_parser("status", help="show a compact runtime summary")
    status.add_argument(
        "--team",
        action="store_true",
        help=(
            "the team as one picture: held tickets, workers and what they are doing, "
            "pull requests, the lane, blockers, the last round, what waits on a person"
        ),
    )
    status.add_argument(
        "--json", action="store_true", help="the --team facts, machine-readable (implies --team)"
    )
    status.set_defaults(func=_cmd_status)

    tail = sub.add_parser(
        "tail", help="the daemon's event stream, one line per event (pickups, notes, rounds, ...)"
    )
    tail.add_argument(
        "--since", default="10m", help="how far back to start: 30s, 10m, 2h, 1d (default 10m)"
    )
    tail.add_argument(
        "--follow", "-f", action="store_true", help="keep printing new events until interrupted"
    )
    tail.set_defaults(func=_cmd_tail)

    for name in ("start", "manager"):
        start = sub.add_parser(
            name,
            help="talk to the manager: launch the harness and let it drive ppy for you",
        )
        start.add_argument(
            "objective",
            nargs="?",
            default=None,
            help="optional objective to hand the manager as the first message",
        )
        start.add_argument(
            "--provider",
            default=None,
            choices=["claude", "codex"],
            help="enter as this harness (default: configured manager provider)",
        )
        start.add_argument("--model", default=None, help="override the manager model")
        start.add_argument("--reasoning", default=None, help="override the manager reasoning")
        start.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="print the harness invocation instead of launching it",
        )
        start.set_defaults(func=_cmd_start)

    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """Keep ``ppy task <id>`` working now that ``task`` has subcommands."""
    if len(argv) >= 2 and argv[0] == "task" and argv[1].isdigit():
        return [argv[0], "show", *argv[1:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    if raw and raw[0] == "serve":
        # `serve` is handed the Papaya client's own `listen` flags, verbatim,
        # including ones this runtime does not take. Routing it around the main
        # parser is what makes "ignored with a warning" possible at all: argparse
        # would refuse an unknown flag and exit before `serve` could say anything.
        return _cmd_serve(argparse.Namespace(listen_args=raw[1:]))
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(raw))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
