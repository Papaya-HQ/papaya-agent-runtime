"""Managed provisioning of pinned companion tools.

``ppy setup`` (and ``ppy tools install``) download each pinned companion from
``tools.lock`` into ``.ppy/tools/<tool>/<version>/`` and expose a stable symlink in
``.ppy/tools/bin/``. Artifacts are verified before activation:

- **treehouse** — a Go release archive fetched from GitHub; its sha256 is checked
  against the per-platform digest in ``tools.lock`` (sourced from the release's
  ``checksums.txt``).
- **lavish-axi**, **gh-axi** — npm packages installed at an exact pinned version
  into an isolated prefix; npm verifies registry integrity on install.

Provisioning is best-effort and non-fatal: a companion that fails to install
leaves Papaya Agent Runtime on its documented fallback (git worktrees; the local
``ppy artifact`` review surface; plain ``gh``). The side-effecting primitives are
small and injectable so the orchestration is unit-testable without a network.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import papaya_agent_runtime
from papaya_agent_runtime.paths import tools_bin_dir, tools_dir


class ProvisionError(Exception):
    pass


@dataclass
class ProvisionResult:
    name: str
    version: str | None
    status: str  # "installed" | "present" | "skipped" | "failed"
    path: str | None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "path": self.path,
            "detail": self.detail,
        }


def repo_root() -> Path:
    """Papaya Agent Runtime repository root (where ``tools.lock`` lives)."""
    return Path(papaya_agent_runtime.__file__).resolve().parents[2]


def lock_path() -> Path:
    return repo_root() / "tools.lock"


def load_lock() -> dict:
    path = lock_path()
    if not path.exists():
        raise ProvisionError(f"tools.lock not found at {path}")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def platform_slug() -> str:
    """Return e.g. ``darwin-arm64`` / ``linux-amd64`` for release asset selection."""
    system = platform.system().lower()  # darwin | linux | windows
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if system not in ("darwin", "linux") or arch is None:
        raise ProvisionError(f"unsupported platform {system}/{machine} for managed provisioning")
    return f"{system}-{arch}"


# --------------------------------------------------------------------------- #
# Injectable side effects (patched in hermetic tests)
# --------------------------------------------------------------------------- #


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tf:
        tf.extractall(dest, filter="data")


def _npm_install(name: str, version: str, prefix: Path) -> None:
    prefix.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["npm", "install", "--prefix", str(prefix), "--no-audit", "--no-fund", f"{name}@{version}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ProvisionError(f"npm install {name}@{version} failed: {proc.stderr.strip()[:300]}")


# --------------------------------------------------------------------------- #
# Symlink activation
# --------------------------------------------------------------------------- #


def _activate(name: str, target: Path) -> Path:
    """Point ``.ppy/tools/bin/<name>`` at ``target`` (idempotent)."""
    bindir = tools_bin_dir()
    bindir.mkdir(parents=True, exist_ok=True)
    link = bindir / name
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    return link


def _find_executable(root: Path, name: str) -> Path | None:
    """Locate an executable named ``name`` anywhere under ``root``."""
    for candidate in root.rglob(name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Per-tool provisioning
# --------------------------------------------------------------------------- #


def provision_treehouse(lock: dict, *, force: bool = False) -> ProvisionResult:
    spec = lock.get("tool", {}).get("treehouse")
    if not spec:
        return ProvisionResult("treehouse", None, "failed", None, "no pin in tools.lock")
    version = spec["version"]
    slug = platform_slug()
    dest = tools_dir() / "treehouse" / version
    binary = dest / "treehouse"

    if binary.exists() and not force:
        _activate("treehouse", binary)
        return ProvisionResult("treehouse", version, "present", str(binary))

    sha_key = f"sha256_{slug.replace('-', '_')}"
    expected = spec.get(sha_key)
    if not expected:
        return ProvisionResult(
            "treehouse", version, "failed", None, f"no checksum for {slug} in tools.lock"
        )
    asset = f"treehouse-{version}-{slug}.tar.gz"
    url = f"{spec['source']}/releases/download/{version}/{asset}"

    tmp = dest / asset
    try:
        _download(url, tmp)
        actual = _sha256(tmp)
        if actual != expected:
            tmp.unlink(missing_ok=True)
            return ProvisionResult(
                "treehouse",
                version,
                "failed",
                None,
                f"checksum mismatch: expected {expected[:12]}…, got {actual[:12]}…",
            )
        _extract_tar(tmp, dest)
        tmp.unlink(missing_ok=True)
        found = _find_executable(dest, "treehouse")
        if found is None:
            return ProvisionResult(
                "treehouse", version, "failed", None, "treehouse binary not found in archive"
            )
        if found != binary:
            shutil.copy2(found, binary)
            binary.chmod(0o755)
        link = _activate("treehouse", binary)
    except (OSError, ProvisionError) as exc:
        return ProvisionResult("treehouse", version, "failed", None, str(exc))
    return ProvisionResult("treehouse", version, "installed", str(link))


def provision_npm_tool(name: str, lock: dict, *, force: bool = False) -> ProvisionResult:
    spec = lock.get("tool", {}).get(name)
    if not spec:
        return ProvisionResult(name, None, "failed", None, "no pin in tools.lock")
    version = spec["version"]
    prefix = tools_dir() / name / version
    bin_link = prefix / "node_modules" / ".bin" / name

    if bin_link.exists() and not force:
        link = _activate(name, bin_link)
        return ProvisionResult(name, version, "present", str(link))

    if shutil.which("npm") is None:
        return ProvisionResult(name, version, "failed", None, "npm not found (install Node 22+)")
    try:
        _npm_install(name, version, prefix)
        if not bin_link.exists():
            found = _find_executable(prefix / "node_modules", name)
            if found is None:
                return ProvisionResult(name, version, "failed", None, "installed but bin not found")
            bin_link = found
        link = _activate(name, bin_link)
    except (OSError, ProvisionError) as exc:
        return ProvisionResult(name, version, "failed", None, str(exc))
    return ProvisionResult(name, version, "installed", str(link))


_PROVISIONERS = {
    "treehouse": provision_treehouse,
    "lavish-axi": lambda lock, *, force=False: provision_npm_tool("lavish-axi", lock, force=force),
    "gh-axi": lambda lock, *, force=False: provision_npm_tool("gh-axi", lock, force=force),
}


def provision_all(
    *, names: list[str] | None = None, force: bool = False, lock: dict | None = None
) -> list[ProvisionResult]:
    """Provision the requested companions (all by default). Never raises."""
    lock = lock if lock is not None else load_lock()
    targets = names or list(_PROVISIONERS)
    results: list[ProvisionResult] = []
    for name in targets:
        provisioner = _PROVISIONERS.get(name)
        if provisioner is None:
            results.append(ProvisionResult(name, None, "failed", None, "unknown companion"))
            continue
        try:
            results.append(provisioner(lock, force=force))
        except Exception as exc:  # noqa: BLE001 - provisioning must never crash setup
            results.append(ProvisionResult(name, None, "failed", None, repr(exc)))
    return results
