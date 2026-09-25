"""CLI for the M0 provider-capability probes.

Usage:
    python -m papaya_agent_runtime.probes run [--provider claude|codex] [--out PATH]

By default it probes every detected, authenticated provider, writes raw evidence
under ``.ppy/probes/<run-id>/`` (gitignored), a machine-readable
``capabilities.json`` alongside it, and the machine-local record adapters read at
``.ppy/provider-capabilities.json``. Nothing tracked by git changes.

Pass ``--publish`` to additionally refresh the tracked repo-root
``provider-capabilities.json`` and the human matrix at
``docs/provider-capabilities.md``. That is a maintainer action: those files are
shared, so publishing replaces whatever versions someone else probed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from papaya_agent_runtime.paths import local_capabilities_path
from papaya_agent_runtime.probes.providers import (
    default_models,
    detect_claude,
    detect_codex,
)
from papaya_agent_runtime.probes.report import carried_records, render_markdown
from papaya_agent_runtime.probes.scenarios import run_provider
from papaya_agent_runtime.providers.capability import clear_cache

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _detect(selected: str | None):
    models = default_models()
    detectors = {"claude": detect_claude, "codex": detect_codex}
    specs = []
    for name, detector in detectors.items():
        if selected and selected != name:
            continue
        spec = detector(models[name])
        if spec is None:
            print(f"[probe] {name}: not installed or not detectable; skipping")
            continue
        specs.append(spec)
    return specs


def _cmd_run(args: argparse.Namespace) -> int:
    specs = _detect(args.provider)
    if not specs:
        print("[probe] no providers detected; nothing to do", file=sys.stderr)
        return 1

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = os.path.join(_REPO_ROOT, ".ppy", "probes", run_id)
    os.makedirs(evidence_root, exist_ok=True)

    records = []
    for spec in specs:
        print(f"[probe] running {spec.name} {spec.version} (model={spec.model})")
        evidence_dir = os.path.join(evidence_root, spec.name)
        record = run_provider(spec, evidence_dir)
        records.append(record)
        proved = [f for f in record.capabilities.to_dict() if getattr(record.capabilities, f)]
        print(f"[probe] {spec.name}: proved {sorted(proved) or 'nothing'}")

    machine_path = os.path.join(evidence_root, "capabilities.json")
    with open(machine_path, "w", encoding="utf-8") as fh:
        json.dump([r.to_dict() for r in records], fh, indent=2)
    print(f"[probe] wrote machine record: {machine_path}")

    providers = {
        r.provider: {
            "cli_version": r.cli_version,
            "model": r.model,
            "probed_at": r.probed_at,
            "capabilities": r.capabilities.to_dict(),
        }
        for r in records
    }

    # The record adapters actually read: machine-local, gitignored, and merged
    # per provider over the tracked one. Probing only claude leaves the tracked
    # codex row standing rather than demoting it to unproven.
    local_path = local_capabilities_path()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    merged = _load_providers(str(local_path))
    merged.update(providers)
    _write_record(
        str(local_path),
        merged,
        "Machine-local provider capability matrix for THIS machine, written by "
        "`python -m papaya_agent_runtime.probes run`. Overrides the tracked repo-root "
        "record per provider. Gitignored: never commit this.",
    )
    print(f"[probe] wrote machine-local record: {local_path}")
    clear_cache()

    if not args.publish:
        print("[probe] tracked files untouched (pass --publish to refresh them)")
        return 0

    # Merged per provider, like the local record: publishing from a machine with
    # only claude installed refreshes claude and leaves codex as last proved.
    committed = os.path.join(_REPO_ROOT, "provider-capabilities.json")
    tracked = _load_providers(committed)
    matrix = os.path.join(_REPO_ROOT, "docs", "provider-capabilities.md")
    previous = Path(matrix).read_text(encoding="utf-8") if os.path.exists(matrix) else ""
    carried = carried_records(tracked, previous, probed=set(providers))
    published = sorted(records + carried, key=lambda r: r.provider)

    out_path = args.out or matrix
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(published))
    print(f"[probe] wrote capability matrix: {out_path}")
    if carried:
        print(f"[probe] kept unprobed providers as published: {[r.provider for r in carried]}")

    # Refresh the committed machine-readable record that adapters consume (M3).
    tracked.update(providers)
    _write_record(
        committed,
        tracked,
        "Machine-readable provider capability matrix consumed by adapters. "
        "Generated by `python -m papaya_agent_runtime.probes run --publish`. "
        "Fail-closed: a missing provider or flag reads as false.",
    )
    print(f"[probe] refreshed committed record: {committed}")
    return 0


def _load_providers(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("providers", {})
    except (ValueError, OSError):
        return {}


def _write_record(path: str, providers: dict, note: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"note": note, "providers": providers}, fh, indent=2)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m papaya_agent_runtime.probes")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the live interrupt/resume probe matrix")
    run.add_argument(
        "--provider",
        choices=["claude", "codex"],
        default=None,
        help="probe only this provider (default: all detected)",
    )
    run.add_argument("--out", default=None, help="path for the capability matrix markdown")
    run.add_argument(
        "--publish",
        action="store_true",
        help="also refresh the tracked provider-capabilities.json and docs matrix "
        "(maintainer action: these are shared files)",
    )
    run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
