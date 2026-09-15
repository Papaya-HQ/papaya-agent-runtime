"""Environment discovery for setup, config, and doctor.

Probes installed executables, versions, and authentication state. Only detected,
authenticated harnesses may be selected as manager or worker; unavailable ones
remain visible with a repair hint but cannot be chosen.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, dataclass

from papaya_agent_runtime.paths import tools_bin_dir


@dataclass
class Tool:
    name: str
    kind: str  # "harness" | "requirement" | "companion"
    path: str | None
    version: str | None
    authenticated: bool | None  # None when auth does not apply
    available: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _run(argv: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _version(argv: list[str]) -> str | None:
    code, out = _run(argv)
    if code != 0 or not out.strip():
        return None
    return out.strip().splitlines()[0].strip()


def detect_claude() -> Tool:
    path = _which("claude")
    if not path:
        return Tool(
            "claude",
            "harness",
            None,
            None,
            None,
            False,
            "not installed; install Claude Code, then `claude auth login`",
        )
    version = _version(["claude", "--version"])
    code, out = _run(["claude", "auth", "status", "--text"])
    authed = code == 0
    detail = "" if authed else "run `claude auth login`"
    return Tool("claude", "harness", path, version, authed, authed, detail)


def detect_codex() -> Tool:
    path = _which("codex")
    if not path:
        return Tool(
            "codex",
            "harness",
            None,
            None,
            None,
            False,
            "not installed; install the Codex CLI and sign in",
        )
    version = _version(["codex", "--version"])
    code, out = _run(["codex", "login", "status"])
    authed = code == 0 and "logged in" in out.lower()
    detail = "" if authed else "run `codex login`"
    return Tool("codex", "harness", path, version, authed, authed, detail)


def detect_requirement(name: str, version_argv: list[str], hint: str) -> Tool:
    path = _which(name)
    if not path:
        return Tool(name, "requirement", None, None, None, False, hint)
    version = _version(version_argv)
    return Tool(name, "requirement", path, version, None, True)


def detect_companion(name: str) -> Tool:
    """Report a companion, preferring the copy provisioned under .ppy/tools/bin."""
    from papaya_agent_runtime.companions import companion_bin

    path = companion_bin(name)
    if not path:
        return Tool(
            name,
            "companion",
            None,
            None,
            None,
            False,
            "optional; install with `ppy tools install`",
        )
    managed = str(tools_bin_dir()) in path
    detail = "managed (.ppy/tools)" if managed else "on PATH"
    return Tool(name, "companion", path, _version([path, "--version"]), None, True, detail)


def discover() -> dict[str, list[dict]]:
    harnesses = [detect_claude(), detect_codex()]
    requirements = [
        detect_requirement("git", ["git", "--version"], "install Git"),
        detect_requirement("python3", ["python3", "--version"], "install Python 3.12+"),
        detect_requirement("node", ["node", "--version"], "install Node 22+"),
        detect_requirement("gh", ["gh", "--version"], "install the GitHub CLI"),
        detect_requirement("uv", ["uv", "--version"], "install uv"),
    ]
    companions = [
        detect_companion("treehouse"),
        detect_companion("gh-axi"),
        detect_companion("lavish-axi"),
    ]
    return {
        "harnesses": [t.to_dict() for t in harnesses],
        "requirements": [t.to_dict() for t in requirements],
        "companions": [t.to_dict() for t in companions],
    }


def usable_harnesses(report: dict[str, list[dict]]) -> list[str]:
    return [h["name"] for h in report["harnesses"] if h["available"]]
