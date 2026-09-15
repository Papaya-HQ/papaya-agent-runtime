"""The receipt helper runs in task context and retains output plus one result line."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from papaya_agent_runtime import cli, environment, receipt, repos
from papaya_agent_runtime.state import init_db, store


def _task(source_repo: str) -> tuple[int, Path]:
    added = repos.add_repo(source_repo, name="backend")
    repos.set_settings(
        added.name,
        compose_stack="yes",
        db_port_base="54000",
        db_port_variable="PAPAYA_DB_PORT",
        db_url_template="postgresql://localhost:{port}/{name}_{task_id}",
        test_db_url_template="postgresql://localhost:{port}/{name}_{task_id}_test",
    )
    conn = init_db()
    repo_row = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "collect proof")
    task_id = store.add_task(conn, run_id=run_id, title="gate", repo_id=repo_row["id"])
    store.update_task_fields(
        conn,
        task_id,
        worktree_path=source_repo,
        base_sha=subprocess.run(
            ["git", "-C", source_repo, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    )
    environment.prepare(
        conn, repo_row, task_id=task_id, worktree=source_repo, branch="ppy/task-receipt"
    )
    return task_id, Path(source_repo) / ".ppy-evidence"


def test_receipt_tees_task_environment_and_uses_injected_clock(
    ppy_home, source_repo, monkeypatch, capsys
) -> None:
    task_id, evidence = _task(source_repo)
    monkeypatch.setenv("VIRTUAL_ENV", "/manager/.venv")
    monkeypatch.setenv("DATABASE_URL", "manager-dev")
    monkeypatch.setenv("TEST_DATABASE_URL", "manager-test")
    code = (
        "import json, os; print(json.dumps({k: os.environ.get(k) for k in "
        "['COMPOSE_PROJECT_NAME','PAPAYA_DB_PORT','DATABASE_URL','TEST_DATABASE_URL',"
        "'UV_CACHE_DIR','RUFF_CACHE_DIR','MYPY_CACHE_DIR','VIRTUAL_ENV']}))"
    )
    ticks = iter([10.0, 12.5])
    result = receipt.run(
        task_id,
        [sys.executable, "-c", code],
        monotonic=lambda: next(ticks),
        utc_now=lambda: datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
    )

    payload = json.loads(Path(result.output_path).read_text())
    assert json.loads(capsys.readouterr().out) == payload
    assert payload["COMPOSE_PROJECT_NAME"] == f"task_{task_id}"
    assert payload["PAPAYA_DB_PORT"] == str(54000 + task_id)
    assert payload["DATABASE_URL"].endswith(f"/backend_{task_id}")
    assert payload["TEST_DATABASE_URL"].endswith(f"/backend_{task_id}_test")
    assert payload["VIRTUAL_ENV"] is None
    assert Path(result.output_path).suffix == ".txt"
    assert result.elapsed == 2.5
    assert result.result_line.endswith(f"| 2.500s | head={result.head_sha}")
    assert (evidence / "receipts.txt").read_text() == result.result_line + "\n"


def test_receipt_cli_propagates_failure_and_appends_one_line(ppy_home, source_repo, capsys) -> None:
    task_id, evidence = _task(source_repo)
    assert (
        cli.main(["receipt", str(task_id), "--", sys.executable, "-c", "raise SystemExit(7)"]) == 7
    )
    assert "receipt:" in capsys.readouterr().out
    lines = (evidence / "receipts.txt").read_text().splitlines()
    assert len(lines) == 1
    assert "raise SystemExit(7)" in lines[0]
    assert "exit=7" in lines[0]
