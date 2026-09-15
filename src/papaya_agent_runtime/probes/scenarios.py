"""Probe scenarios that derive a fail-closed capability record.

Each scenario spawns a real provider CLI in an isolated git fixture, applies an
interrupt where relevant, attempts a resume, and records evidence. Capabilities
are only ever set true on explicit proof.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from datetime import UTC, datetime

from papaya_agent_runtime import __version__
from papaya_agent_runtime.probes.capability import (
    Capabilities,
    CapabilityRecord,
    ScenarioResult,
)
from papaya_agent_runtime.probes.fixtures import add_worktree, git_dirty, make_repo
from papaya_agent_runtime.probes.process import StreamResult, run_streaming
from papaya_agent_runtime.probes.providers import ProviderSpec


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_evidence(evidence_dir: str, name: str, result: StreamResult) -> str:
    os.makedirs(evidence_dir, exist_ok=True)
    path = os.path.join(evidence_dir, f"{name}.log")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"argv: {result.argv}\n")
        fh.write(f"exit_code: {result.exit_code}\n")
        fh.write(f"timed_out: {result.timed_out}\n")
        fh.write(f"interrupted: {result.interrupted} signal={result.signal_sent}\n")
        fh.write(f"duration_s: {result.duration_s:.2f}\n")
        fh.write("--- stdout ---\n")
        fh.write(result.stdout + "\n")
        fh.write("--- stderr ---\n")
        fh.write(result.stderr + "\n")
    return path


def _resume_ok(spec: ProviderSpec, result: StreamResult) -> bool:
    if result.timed_out or result.exit_code != 0:
        return False
    haystack = (result.stdout + "\n" + result.stderr).lower()
    return not any(marker.lower() in haystack for marker in spec.notfound_markers)


def _pgrep(pattern: str) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(p) for p in out.stdout.split() if p.strip().isdigit()]


def run_provider(spec: ProviderSpec, evidence_dir: str) -> CapabilityRecord:
    caps = Capabilities()
    scenarios: list[ScenarioResult] = []
    caps.requires_resume_prompt = spec.requires_resume_prompt

    # ------------------------------------------------------------------ #
    # Scenario 1: clean completion, then resume / duplicate / worktree.
    # ------------------------------------------------------------------ #
    fixture = make_repo()
    try:
        base = run_streaming(
            spec.base_argv(spec.trivial_prompt, fixture.root, spec.model),
            cwd=fixture.root,
            timeout_s=120,
        )
        _write_evidence(evidence_dir, f"{spec.name}-clean-base", base)
        sid = spec.extract_session_id(base.lines)
        caps.session_id_in_stream = caps.session_id_in_stream or bool(sid)
        caps.usage_in_stream = caps.usage_in_stream or spec.has_usage(base.lines)

        if base.exit_code == 0 and sid:
            scenarios.append(
                ScenarioResult(
                    "clean_complete",
                    "proved",
                    f"clean run exited 0 with session id {sid}",
                    sid,
                    base.duration_s,
                )
            )
            resume = run_streaming(
                spec.resume_argv(sid, spec.resume_prompt, spec.model),
                cwd=fixture.root,
                timeout_s=120,
            )
            _write_evidence(evidence_dir, f"{spec.name}-clean-resume", resume)
            if _resume_ok(spec, resume):
                caps.resume_after_clean_exit = True
                caps.session_survives_process_exit = True
                scenarios.append(
                    ScenarioResult(
                        "resume_after_clean_exit",
                        "proved",
                        "resume by id exited 0 with no not-found marker",
                        sid,
                        resume.duration_s,
                    )
                )

                dup = run_streaming(
                    spec.resume_argv(sid, spec.resume_prompt, spec.model),
                    cwd=fixture.root,
                    timeout_s=120,
                )
                _write_evidence(evidence_dir, f"{spec.name}-duplicate-resume", dup)
                if _resume_ok(spec, dup):
                    caps.duplicate_resume_ok = True
                scenarios.append(
                    ScenarioResult(
                        "duplicate_resume",
                        "proved" if caps.duplicate_resume_ok else "disproved",
                        "second resume of the same id "
                        + ("succeeded" if caps.duplicate_resume_ok else "failed"),
                        sid,
                        dup.duration_s,
                    )
                )

                try:
                    worktree = add_worktree(fixture)
                    wt = run_streaming(
                        spec.resume_argv(sid, spec.resume_prompt, spec.model),
                        cwd=worktree,
                        timeout_s=120,
                    )
                    _write_evidence(evidence_dir, f"{spec.name}-worktree-resume", wt)
                    if _resume_ok(spec, wt):
                        caps.worktree_resume = True
                    scenarios.append(
                        ScenarioResult(
                            "worktree_resume",
                            "proved" if caps.worktree_resume else "disproved",
                            "resume from a linked worktree "
                            + ("succeeded" if caps.worktree_resume else "failed"),
                            sid,
                            wt.duration_s,
                        )
                    )
                except (OSError, subprocess.CalledProcessError) as exc:
                    scenarios.append(
                        ScenarioResult("worktree_resume", "error", f"worktree setup failed: {exc}")
                    )
            else:
                scenarios.append(
                    ScenarioResult(
                        "resume_after_clean_exit",
                        "disproved",
                        f"resume exited {resume.exit_code}; timed_out={resume.timed_out}",
                        sid,
                        resume.duration_s,
                    )
                )
        else:
            scenarios.append(
                ScenarioResult(
                    "clean_complete",
                    "inconclusive",
                    f"base exit={base.exit_code} sid={'yes' if sid else 'no'}",
                    sid,
                    base.duration_s,
                )
            )
    except Exception as exc:  # noqa: BLE001 - one scenario must not abort the matrix
        scenarios.append(ScenarioResult("clean_complete", "error", repr(exc)))
    finally:
        fixture.cleanup()

    # ------------------------------------------------------------------ #
    # Scenario 2: SIGINT during a model turn (before any tool).
    # ------------------------------------------------------------------ #
    scenarios.append(
        _interrupt_then_resume(
            spec,
            evidence_dir,
            label="sigint_model_turn",
            cap_setter=lambda: setattr(caps, "resume_after_sigint_model_turn", True),
            prompt=spec.trivial_prompt,
            predicate=spec.model_turn_predicate,
            interrupt_after_s=6.0,
            interrupt_signal=signal.SIGINT,
            caps=caps,
        )
    )

    # ------------------------------------------------------------------ #
    # Scenario 3: SIGINT during a long tool command (sleep).
    # ------------------------------------------------------------------ #
    scenarios.append(_sigint_tool(spec, evidence_dir, caps))

    # ------------------------------------------------------------------ #
    # Scenario 4: SIGKILL the process group early, then attempt resume.
    # ------------------------------------------------------------------ #
    scenarios.append(
        _interrupt_then_resume(
            spec,
            evidence_dir,
            label="sigkill_survival",
            cap_setter=lambda: setattr(caps, "resume_after_sigkill", True),
            prompt=spec.trivial_prompt,
            predicate=None,
            interrupt_after_s=3.0,
            interrupt_signal=signal.SIGKILL,
            caps=caps,
        )
    )

    # ------------------------------------------------------------------ #
    # Scenario 5: mid-process steer (fail-closed; provider-specific).
    # ------------------------------------------------------------------ #
    scenarios.append(_mid_process_steer(spec, evidence_dir, caps))

    return CapabilityRecord(
        provider=spec.name,
        cli_version=spec.version,
        model=spec.model,
        probed_at=_now(),
        probe_tool_version=__version__,
        capabilities=caps,
        scenarios=scenarios,
    )


def _interrupt_then_resume(
    spec: ProviderSpec,
    evidence_dir: str,
    *,
    label: str,
    cap_setter,
    prompt: str,
    predicate,
    interrupt_after_s: float,
    interrupt_signal: signal.Signals,
    caps: Capabilities,
) -> ScenarioResult:
    fixture = make_repo()
    try:
        run = run_streaming(
            spec.base_argv(prompt, fixture.root, spec.model),
            cwd=fixture.root,
            interrupt_predicate=predicate,
            interrupt_after_s=interrupt_after_s,
            interrupt_signal=interrupt_signal,
            grace_s=8,
            timeout_s=90,
        )
        _write_evidence(evidence_dir, f"{spec.name}-{label}-base", run)
        sid = spec.extract_session_id(run.lines)
        caps.session_id_in_stream = caps.session_id_in_stream or bool(sid)
        if not run.interrupted:
            return ScenarioResult(
                label, "inconclusive", "interrupt never fired", sid, run.duration_s
            )
        if not sid:
            return ScenarioResult(
                label,
                "inconclusive",
                f"interrupted with {interrupt_signal.name} but no session id captured",
                None,
                run.duration_s,
            )
        base_info = (
            f"base exit={run.exit_code} dur={run.duration_s:.1f}s "
            f"interrupt_at_line={run.interrupt_at_line}"
        )
        resume = run_streaming(
            spec.resume_argv(sid, spec.resume_prompt, spec.model),
            cwd=fixture.root,
            timeout_s=120,
        )
        _write_evidence(evidence_dir, f"{spec.name}-{label}-resume", resume)
        if _resume_ok(spec, resume):
            cap_setter()
            return ScenarioResult(
                label,
                "proved",
                f"{base_info}; resumed after {interrupt_signal.name} (resume exit 0)",
                sid,
                resume.duration_s,
            )
        return ScenarioResult(
            label,
            "disproved",
            f"{base_info}; resume after {interrupt_signal.name} exited {resume.exit_code}",
            sid,
            resume.duration_s,
        )
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult(label, "error", repr(exc))
    finally:
        fixture.cleanup()


def _sigint_tool(spec: ProviderSpec, evidence_dir: str, caps: Capabilities) -> ScenarioResult:
    fixture = make_repo()
    try:
        run = run_streaming(
            spec.base_argv(spec.tool_prompt, fixture.root, spec.model),
            cwd=fixture.root,
            interrupt_predicate=spec.tool_start_predicate,
            interrupt_after_s=45.0,  # ceiling: interrupt even if we miss the tool event
            interrupt_signal=signal.SIGINT,
            interrupt_delay_s=1.0,  # let the sleep actually start
            grace_s=8,
            timeout_s=90,
        )
        _write_evidence(evidence_dir, f"{spec.name}-sigint_tool-base", run)
        sid = spec.extract_session_id(run.lines)
        caps.session_id_in_stream = caps.session_id_in_stream or bool(sid)

        # Did the tool child survive the process-group signal?
        time.sleep(1.0)
        survivors = _pgrep("sleep 20")
        if survivors:
            caps.orphan_tool_process = True
            for pid in survivors:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        dirty = git_dirty(fixture.root)

        detail = (
            f"base exit={run.exit_code} dur={run.duration_s:.1f}s; "
            f"interrupted={run.interrupted}; orphan_sleep={bool(survivors)}; "
            f"worktree_dirty={dirty}"
        )
        if not run.interrupted or not sid:
            return ScenarioResult(
                "sigint_tool",
                "inconclusive",
                detail + f"; sid={'yes' if sid else 'no'}",
                sid,
                run.duration_s,
            )
        resume = run_streaming(
            spec.resume_argv(sid, spec.resume_prompt, spec.model),
            cwd=fixture.root,
            timeout_s=120,
        )
        _write_evidence(evidence_dir, f"{spec.name}-sigint_tool-resume", resume)
        if _resume_ok(spec, resume):
            caps.resume_after_sigint_tool = True
            return ScenarioResult(
                "sigint_tool", "proved", detail + "; resume exit 0", sid, resume.duration_s
            )
        return ScenarioResult(
            "sigint_tool",
            "disproved",
            detail + f"; resume exit {resume.exit_code}",
            sid,
            resume.duration_s,
        )
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult("sigint_tool", "error", repr(exc))
    finally:
        fixture.cleanup()


def _mid_process_steer(spec: ProviderSpec, evidence_dir: str, caps: Capabilities) -> ScenarioResult:
    """Best-effort check for accepting a follow-up into the same process.

    Codex exec is unidirectional (stdin is /dev/null), so it is disproved by
    design. Claude print mode exposes --input-format stream-json; we attempt a
    two-message stdin session and only claim success on clear evidence.
    """
    if spec.name == "codex":
        return ScenarioResult(
            "mid_process_steer",
            "disproved",
            "codex exec is single-shot; stdin is /dev/null and resume starts a new turn",
        )

    if spec.name == "claude":
        fixture = make_repo()
        try:
            import json

            msgs = [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Reply with the single word ONE.",
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Now reply with the single word TWO.",
                    },
                },
            ]
            stdin_data = "".join(json.dumps(m) + "\n" for m in msgs)
            argv = [
                "claude",
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
            ]
            if spec.model:
                argv += ["--model", spec.model]
            run = run_streaming(
                argv,
                cwd=fixture.root,
                stdin_data=stdin_data,
                timeout_s=90,
            )
            _write_evidence(evidence_dir, f"{spec.name}-mid_process_steer", run)
            both = ("ONE" in run.stdout) and ("TWO" in run.stdout)
            if run.exit_code == 0 and both:
                caps.mid_process_steer = True
                return ScenarioResult(
                    "mid_process_steer",
                    "proved",
                    "streamed two user messages into one process; both answered",
                    duration_s=run.duration_s,
                )
            return ScenarioResult(
                "mid_process_steer",
                "inconclusive",
                f"exit={run.exit_code} saw_ONE={'ONE' in run.stdout} saw_TWO={'TWO' in run.stdout}",
                duration_s=run.duration_s,
            )
        except Exception as exc:  # noqa: BLE001
            return ScenarioResult("mid_process_steer", "error", repr(exc))
        finally:
            fixture.cleanup()

    return ScenarioResult("mid_process_steer", "skipped", "unknown provider")
