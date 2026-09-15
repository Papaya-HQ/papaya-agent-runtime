"""Smoke test for the bundled `lavish-review` script.

Uses a fake `lavish-axi` on PATH to prove readiness is established before the poll
is backgrounded, a vanished session is retried, and drain keeps poller errors apart
from feedback — without a real tool and without ever blocking the caller.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import scale

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "skills"
    / "review-surfaces"
    / "scripts"
    / "lavish-review"
)

FAKE_LAVISH = r"""#!/usr/bin/env bash
state="${FAKE_LAVISH_STATE:?}"
if [ "$1" = "poll" ]; then
  count_file="$state/poll-count"
  count=$(cat "$count_file" 2>/dev/null || echo 0)
  count=$((count + 1))
  echo "$count" >"$count_file"
  if { [ "${FAKE_POLL_MODE:-feedback}" = "not-found-once" ] && [ "$count" -eq 1 ]; } \
    || [ "${FAKE_POLL_MODE:-feedback}" = "always-not-found" ]; then
    echo "error: NOT_FOUND: No active Lavish Editor session for this file"
    exit 1
  fi
  echo "annotation: tighten the header"
  sleep 30
  exit 0
fi

count_file="$state/open-count"
count=$(cat "$count_file" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" >"$count_file"
echo "session:"
echo "  file: $1"
if [ "${FAKE_NEVER_READY:-0}" = "1" ] || [ "$count" -lt 2 ]; then
  echo "  status: starting"
else
  echo "  status: opened"
fi
exit 0
"""


@pytest.fixture
def env_with_fake_lavish(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    fake = bindir / "lavish-axi"
    fake.write_text(FAKE_LAVISH)
    fake.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        FAKE_LAVISH_STATE=str(state),
        LAVISH_REVIEW_READY_ATTEMPTS="3",
        LAVISH_REVIEW_POLL_ATTEMPTS="3",
        LAVISH_REVIEW_RETRY_DELAY="0.01",
    )
    return env


def _run(args, env, cwd):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=scale(10),
    )


def _poller_pid(surface):
    return int(surface.with_name(f".{surface.name}.pid").read_text().strip())


def _process_is_alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def test_open_waits_for_session_then_reuses_live_poller(tmp_path, env_with_fake_lavish):
    surface = tmp_path / "map.html"
    surface.write_text("<html></html>")
    env, cwd = env_with_fake_lavish, str(tmp_path)
    surface.with_name(f".{surface.name}.pid").write_text("999999")

    try:
        opened = _run(["open", str(surface)], env, cwd)
        # open returns immediately (well under the 10s timeout) and hands the turn back.
        assert opened.returncode == 0, opened.stderr
        assert "background" in opened.stdout
        assert "hand the turn back" in opened.stdout
        assert (tmp_path / "state" / "open-count").read_text().strip() == "2"
        pid = _poller_pid(surface)
        assert _process_is_alive(pid)

        reopened = _run(["open", str(surface)], env, cwd)
        assert reopened.returncode == 0, reopened.stderr
        assert "already polling" in reopened.stdout
        assert _poller_pid(surface) == pid
        assert (tmp_path / "state" / "open-count").read_text().strip() == "2"

        # Poller writes asynchronously; give it a beat, then drain non-blockingly.
        annotation_seen = False
        for _ in range(20):
            drained = _run(["drain", str(surface)], env, cwd)
            if "annotation: tighten the header" in drained.stdout:
                annotation_seen = True
                break
            time.sleep(0.1)
        assert annotation_seen, "drain never surfaced the backgrounded feedback"

        stopped = _run(["stop", str(surface)], env, cwd)
        assert "poller stopped" in stopped.stdout
    finally:
        pidf = surface.with_name(f".{surface.name}.pid")
        if pidf.exists():
            with contextlib.suppress(ValueError, ProcessLookupError, OSError):
                os.kill(int(pidf.read_text().strip()), 15)


def test_poller_reopens_and_retries_once_after_not_found(tmp_path, env_with_fake_lavish):
    surface = tmp_path / "retry.html"
    surface.write_text("<html></html>")
    env = dict(env_with_fake_lavish, FAKE_POLL_MODE="not-found-once")

    try:
        opened = _run(["open", str(surface)], env, str(tmp_path))
        assert opened.returncode == 0, opened.stderr

        for _ in range(50):
            count_file = tmp_path / "state" / "poll-count"
            if count_file.exists() and count_file.read_text().strip() == "2":
                break
            time.sleep(0.02)
        else:
            pytest.fail("poller did not retry after NOT_FOUND")

        assert _process_is_alive(_poller_pid(surface))
    finally:
        _run(["stop", str(surface)], env, str(tmp_path))


def test_open_reports_bounded_readiness_failure(tmp_path, env_with_fake_lavish):
    surface = tmp_path / "never-ready.html"
    surface.write_text("<html></html>")
    env = dict(env_with_fake_lavish, FAKE_NEVER_READY="1")

    started = time.monotonic()
    opened = _run(["open", str(surface)], env, str(tmp_path))

    assert opened.returncode == 1
    assert time.monotonic() - started < 1
    assert "did not report a ready session after 3 attempts" in opened.stderr
    assert (tmp_path / "state" / "open-count").read_text().strip() == "3"
    assert not surface.with_name(f".{surface.name}.pid").exists()


def test_poller_not_found_retries_are_bounded(tmp_path, env_with_fake_lavish):
    surface = tmp_path / "vanished.html"
    surface.write_text("<html></html>")
    env = dict(env_with_fake_lavish, FAKE_POLL_MODE="always-not-found")

    opened = _run(["open", str(surface)], env, str(tmp_path))
    assert opened.returncode == 0, opened.stderr

    pid = _poller_pid(surface)
    for _ in range(50):
        if not _process_is_alive(pid):
            break
        time.sleep(0.02)
    else:
        pytest.fail("poller remained alive after its bounded NOT_FOUND retries")

    assert (tmp_path / "state" / "poll-count").read_text().strip() == "3"


def test_drain_reports_error_only_sidecar_and_dead_poller(tmp_path, env_with_fake_lavish):
    surface = tmp_path / "failed.html"
    surface.write_text("<html></html>")
    surface.with_name(f".{surface.name}.feedback").write_text(
        "error: NOT_FOUND: No active Lavish Editor session for this file\n"
    )

    drained = _run(["drain", str(surface)], env_with_fake_lavish, str(tmp_path))

    assert drained.returncode == 0
    assert "poller errors:" in drained.stdout
    assert "NOT_FOUND" in drained.stdout
    assert "feedback" not in drained.stdout
    assert f"poller is not running; re-arm with `lavish-review open {surface}`" in drained.stdout


def test_drain_before_open_is_empty_and_nonblocking(tmp_path, env_with_fake_lavish):
    surface = tmp_path / "plan.html"
    drained = _run(["drain", str(surface)], env_with_fake_lavish, str(tmp_path))
    assert drained.returncode == 0
    assert "no feedback yet" in drained.stdout


def test_missing_tool_points_at_local_surface(tmp_path):
    surface = tmp_path / "plan.html"
    surface.write_text("x")
    # Minimal PATH with no lavish-axi.
    env = dict(os.environ, PATH="/usr/bin:/bin")
    result = _run(["open", str(surface)], env, str(tmp_path))
    assert result.returncode == 1
    assert "ppy artifact" in result.stderr
