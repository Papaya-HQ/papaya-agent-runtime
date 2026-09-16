"""`ppy doctor` — read-only diagnostics of the environment and config."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from papaya_agent_runtime import capabilities, papaya, readiness
from papaya_agent_runtime.config import ConfigError, load_config
from papaya_agent_runtime.paths import config_path, db_path, ppy_home
from papaya_agent_runtime.providers.capability import LOCAL, record_source, recorded_version
from papaya_agent_runtime.setup.discovery import discover, usable_harnesses

_SEMVER = re.compile(r"\d+\.\d+\.\d+")


def _installed_version(version_str: str | None) -> str | None:
    if not version_str:
        return None
    m = _SEMVER.search(version_str)
    return m.group(0) if m else None


def _capability_drift(report: dict) -> list[dict]:
    """Compare each provider's recorded probe version with the installed CLI.

    A mismatch means the capability record no longer applies and the provider
    should be re-probed (`python -m papaya_agent_runtime.probes run`) before trusting
    interrupt/resume steering. ``source`` names which record answered so the
    reader knows whether a local probe or the tracked file is in play.
    """
    out: list[dict] = []
    for h in report["harnesses"]:
        provider = h["name"]
        recorded = recorded_version(provider)
        if recorded is None:
            continue
        installed = _installed_version(h.get("version"))
        drift = installed is not None and installed != recorded
        out.append(
            {
                "provider": provider,
                "recorded": recorded,
                "installed": installed,
                "drift": drift,
                "source": record_source(provider),
            }
        )
    return out


def _schema_status() -> dict:
    from papaya_agent_runtime.state.db import SCHEMA_VERSION

    status: dict = {
        "path": str(db_path()),
        "present": db_path().exists(),
        "expected": SCHEMA_VERSION,
    }
    if db_path().exists():
        try:
            from papaya_agent_runtime.state import init_db
            from papaya_agent_runtime.state.db import schema_version

            conn = init_db()
            status["version"] = schema_version(conn)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
            status["error"] = str(exc)
    return status


def _repo_forges() -> list[dict]:
    """Every registered repo and whether delivery has somewhere to open a PR.

    A repo registered from a local path before forge URLs existed has none: the
    worker pushes, and ``ppy deliver`` has no forge to open the pull request
    against — which is exactly the hand-push that cost a delivery on 2026-09-04.
    """
    try:
        from papaya_agent_runtime.repos import list_repos

        return [
            {"name": r["name"], "forge_url": r.get("forge_url"), "origin": r.get("origin")}
            for r in list_repos()
        ]
    except Exception:  # noqa: BLE001 - diagnostics must not crash
        return []


def venv_interpreter(venv: Path | None = None, pin: Path | None = None) -> dict:
    """The project environment's interpreter against the series `.python-version` pins.

    `bin/ppy` asks uv for the pinned series, but an environment built before that,
    or by an invoker that could not honour it, can still hold another one — and
    `uv run --no-sync` then runs on it rather than rebuilding under a live `serve`.
    So the mismatch is reported here, as a warning, instead of being fixed silently.
    """
    from papaya_agent_runtime.setup.provision import repo_root

    root = repo_root()
    if venv is None:
        venv = Path(os.environ.get("UV_PROJECT_ENVIRONMENT") or root / ".venv")
    pin = root / ".python-version" if pin is None else pin
    expected = pin.read_text(encoding="utf-8").strip() if pin.is_file() else None
    status: dict = {"path": str(venv), "version": None, "expected": expected, "warning": None}
    cfg = venv / "pyvenv.cfg"
    if not cfg.is_file():
        status["warning"] = "no project environment — the next `ppy` command builds it"
        return status
    for line in cfg.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version_info", "version"):
            status["version"] = value.strip()
            break
    version = status["version"]
    if expected and version != expected and not (version or "").startswith(expected + "."):
        status["warning"] = (
            f"the environment runs Python {version or 'unknown'} but .python-version "
            f"pins {expected}; stop `ppy serve`, then `ppy env sync` rebuilds it"
        )
    return status


def _venv_line(venv: dict) -> str:
    version = venv.get("version") or "?"
    pinned = f" (.python-version {venv['expected']})" if venv.get("expected") else ""
    if venv.get("warning"):
        return f"venv:      {version}{pinned} WARNING — {venv['warning']}"
    return f"venv:      {version}{pinned} [ok]"


def collect() -> dict:
    report = discover()
    home = ppy_home()
    cfg_status: dict = {"path": str(config_path()), "present": config_path().exists()}
    if config_path().exists():
        try:
            cfg = load_config()
            cfg_status["valid"] = True
            cfg_status["manager"] = f"{cfg.manager.provider}/{cfg.manager.model}"
            cfg_status["worker_ceiling"] = f"{cfg.worker.provider}/{cfg.worker.max_model}"
        except ConfigError as exc:
            cfg_status["valid"] = False
            cfg_status["error"] = str(exc)
    verdict = readiness.check()
    return {
        "ppy_home": str(home),
        "readiness": verdict.as_dict(),
        "capabilities": capabilities.collect(),
        "papaya": papaya.status(),
        "venv": venv_interpreter(),
        "config": cfg_status,
        "state_db": _schema_status(),
        "environment": report,
        "usable_harnesses": usable_harnesses(report),
        "capability_drift": _capability_drift(report),
        "repos": _repo_forges(),
    }


def render_text(data: dict) -> str:
    lines: list[str] = []
    lines.append(f"ppy home:   {data['ppy_home']}")
    verdict = data.get("readiness")
    if verdict:
        lines.append(f"readiness: {verdict['state']} — see `ppy readiness` for what and whose")
        for problem in verdict.get("problems", []):
            if problem["code"] == "claude_tools_lack_gate":
                lines.append(f"  WARNING — {problem['summary']}; run {problem['fix']}")
    lines.append(f"client:    {capabilities.client_line(data.get('capabilities'))}")
    connection = data.get("papaya") or {}
    if connection:
        lines.append(f"papaya:    {_papaya_line(connection)}")
    if data.get("venv"):
        lines.append(_venv_line(data["venv"]))
    cfg = data["config"]
    if cfg["present"]:
        state = "valid" if cfg.get("valid") else f"INVALID ({cfg.get('error')})"
        extra = ""
        if cfg.get("valid"):
            extra = f" — manager {cfg['manager']}, worker ceiling {cfg['worker_ceiling']}"
        lines.append(f"config:    {cfg['path']} [{state}]{extra}")
    else:
        lines.append(f"config:    {cfg['path']} [missing — run `ppy setup`]")
    lines.append("")
    lines.append("Harnesses:")
    for h in data["environment"]["harnesses"]:
        mark = "ready" if h["available"] else "unavailable"
        v = h.get("version") or "?"
        detail = f" — {h['detail']}" if h.get("detail") else ""
        lines.append(f"  {h['name']:<8} [{mark}] {v}{detail}")
    lines.append("")
    lines.append("Requirements:")
    for r in data["environment"]["requirements"]:
        mark = "ok" if r["available"] else "MISSING"
        v = r.get("version") or ""
        detail = f" — {r['detail']}" if not r["available"] and r.get("detail") else ""
        lines.append(f"  {r['name']:<8} [{mark}] {v}{detail}")
    lines.append("")
    lines.append("Companion tools:")
    for c in data["environment"]["companions"]:
        mark = "present" if c["available"] else "not provisioned"
        lines.append(f"  {c['name']:<12} [{mark}]")
    drift = data.get("capability_drift", [])
    if drift:
        lines.append("")
        lines.append("Provider capability matrix:")
        for d in drift:
            installed = d["installed"] or "?"
            origin = "local probe" if d.get("source") == LOCAL else "tracked record"
            if d["drift"]:
                lines.append(
                    f"  {d['provider']:<8} DRIFT — {origin} says {d['recorded']}, "
                    f"installed {installed}; re-probe before trusting steering"
                )
            else:
                lines.append(f"  {d['provider']:<8} ok — {origin}, probed at {d['recorded']}")
    repos = data.get("repos", [])
    if repos:
        lines.append("")
        lines.append("Registered repositories:")
        for r in repos:
            if r.get("forge_url"):
                lines.append(f"  {r['name']:<24} forge {r['forge_url']}")
            else:
                lines.append(
                    f"  {r['name']:<24} NO FORGE — deliver cannot open pull requests; "
                    "re-register it with `ppy repo add <path> --forge-url <url>`"
                )
    db = data["state_db"]
    if db.get("present") and db.get("version") is not None:
        mark = "ok" if db.get("version") == db.get("expected") else "MISMATCH"
        lines.append("")
        lines.append(f"State DB schema: v{db['version']} (expected v{db['expected']}) [{mark}]")
    lines.append("")
    usable = data["usable_harnesses"]
    lines.append(f"Usable harnesses: {', '.join(usable) if usable else 'none'}")
    return "\n".join(lines)


def _papaya_line(connection: dict) -> str:
    """The connection, in the terms a person reads it: who, or what is missing.

    The runtime has no identity of its own, so "which agent am I" is the first
    thing a diagnostic should answer — and every answer short of connected has to
    say what would fix it, because none of them stop the runtime working.
    """
    state = connection.get("state")
    if state == "connected":
        who = connection.get("identity") or {}
        role = who.get("role_label") or ""
        return f"connected as {connection.get('addressed')}" + (f" ({role})" if role else "")
    if state == "signed_in":
        return "signed in, no agent pinned — `ppy papaya connect` finishes it"
    if state == "installed":
        return "client installed, not signed in — `ppy papaya connect` signs in"
    return "not connected — the runtime works locally; `ppy papaya connect` adds the workspace"


def run_doctor(as_json: bool = False) -> str:
    data = collect()
    return json.dumps(data, indent=2) if as_json else render_text(data)
