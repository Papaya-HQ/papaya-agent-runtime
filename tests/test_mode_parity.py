"""Nothing that supervises workers is `ppy serve`-only (Shane, 2026-09-17, standing).

The runtime runs as `ppy serve` and as an interactive session, and must work just as
well either way. `papaya_agent_runtime.parity` registers every serve decision point
under a capability. This file is the flag: it fails when serve gains a method or start
remedy the registry does not name, when a shared capability is not reached from both
modes, or when a new serve-only gap appears.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from papaya_agent_runtime import parity

SRC = Path(__file__).resolve().parents[1] / "src" / "papaya_agent_runtime"

#: The classes where serve decides what happens to workers, by file.
SERVE_CLASSES = {"rounds.py": "Rounds", "serve.py": "TicketRunner"}
#: Serve's start-up remedies: run once per `ppy serve` start.
SERVE_START_FUNCTIONS = (
    "self_setup",
    "keep_config_right",
    "keep_state_right",
    "announce_deficiencies",
)


def _methods(filename: str, cls: str) -> set[str]:
    tree = ast.parse((SRC / filename).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {
                f"{cls}.{n.name}"
                for n in node.body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            }
    raise AssertionError(f"{cls} not found in {filename}")


def _serve_surface() -> set[str]:
    names: set[str] = set()
    for filename, cls in SERVE_CLASSES.items():
        names |= _methods(filename, cls)
    names |= {f"serve.{name}" for name in SERVE_START_FUNCTIONS}
    return names


def test_every_serve_decision_point_is_registered_under_a_capability() -> None:
    registered = set(parity.by_serve_name())
    unregistered = sorted(_serve_surface() - {n for n in registered if "." in n})
    assert not unregistered, (
        "serve gained decision points no mode-parity capability covers: "
        f"{unregistered}. Build the behaviour in a module both `ppy serve` and an "
        "interactive session call and register it as shared in "
        "papaya_agent_runtime/parity.py (or, only for the Papaya hold protocol or pure "
        "plumbing, as host or scaffold, with the reason in the capability's summary)."
    )


def test_the_registry_names_nothing_that_no_longer_exists() -> None:
    surface = _serve_surface()
    stale = []
    for name in parity.by_serve_name():
        owner, _, attr = name.partition(".")
        if owner in {cls for cls in SERVE_CLASSES.values()} or name.startswith("serve."):
            if name not in surface:
                stale.append(name)
        else:
            module = importlib.import_module(f"papaya_agent_runtime.{owner}")
            if not hasattr(module, attr):
                stale.append(name)
    assert not stale, f"parity registry names things that are gone: {stale}"


def test_each_serve_name_belongs_to_exactly_one_capability() -> None:
    seen: dict[str, str] = {}
    for capability in parity.CAPABILITIES:
        assert capability.kind in parity.KINDS
        for name in capability.serve:
            assert name not in seen, f"{name} is under {seen[name]} and {capability.name}"
            seen[name] = capability.name


@pytest.mark.parametrize(
    "capability", [c for c in parity.CAPABILITIES if c.kind == parity.SHARED], ids=lambda c: c.name
)
def test_a_shared_capability_is_reached_from_both_modes(capability) -> None:
    importlib.import_module(capability.shared)
    module = capability.shared.rsplit(".", 1)[-1]
    serve_files = {"rounds.py", "serve.py"}
    serve_side = [f for f in serve_files if module in (SRC / f).read_text(encoding="utf-8")]
    assert capability.interactive, f"{capability.name} names no interactive surface"
    missing = [
        m
        for m in capability.interactive
        if module not in (SRC / f"{m.rsplit('.', 1)[-1]}.py").read_text(encoding="utf-8")
    ]
    assert not missing, f"{capability.name}: {missing} never reach {capability.shared}"
    # The serve side reaches it directly or through an interactive-side module it runs
    # (readiness runs in serve's blocker watch; the hooks and heartbeat do not).
    assert serve_side or "papaya_agent_runtime.readiness" in capability.interactive, (
        f"{capability.name}: serve never reaches {capability.shared}"
    )


def test_serve_only_gaps_only_ever_shrink() -> None:
    open_gaps = {c.name for c in parity.gaps()}
    new = sorted(open_gaps - parity.KNOWN_GAPS)
    assert not new, (
        f"new serve-only capabilities {new}: a supervision capability is built shared "
        "from the start (one module both modes call), never added as a gap"
    )
    healed = sorted(parity.KNOWN_GAPS - open_gaps)
    assert not healed, (
        f"{healed} are no longer gaps: remove them from parity.KNOWN_GAPS so they cannot come back"
    )


def test_every_gap_says_what_healing_it_means() -> None:
    for capability in parity.gaps():
        assert capability.heal, f"{capability.name} has no heal line"


def test_open_gaps_are_one_runtime_deficiency_recorded_in_both_modes(ppy_home) -> None:
    from papaya_agent_runtime import deficiencies

    assert parity.record_gaps() is True
    parity.record_gaps()  # every start adds evidence to the one entry
    [row] = [d for d in deficiencies.ledger() if d.kind == deficiencies.SERVE_ONLY_CAPABILITY]
    assert row.count == 2
    assert row.evidence[-1]["where"] == ", ".join(c.name for c in parity.gaps())
    # Both starts call it: serve's start announcement and the session-start hook.
    for filename in ("serve.py", "hooks.py"):
        assert "parity.record_gaps()" in (SRC / filename).read_text(encoding="utf-8")
