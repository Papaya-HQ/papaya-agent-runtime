"""Append-only per-task event spool on disk.

Bulky evidence lives here as JSONL; SQLite carries the queryable index. The spool
is append-only so a crash mid-write never rewrites history, and replay can
reconstruct a task from its spool alone.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from papaya_agent_runtime.paths import runs_dir


class EventSpool:
    def __init__(self, run_id: int | None, task_id: int) -> None:
        base = runs_dir() / (f"run-{run_id}" if run_id is not None else "adhoc")
        self.dir = base / f"task-{task_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self._lock = threading.Lock()

    def append(self, kind: str, payload: dict) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "payload": payload,
        }
        line = json.dumps(record)
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


def spool_path(run_id: int | None, task_id: int) -> Path:
    base = runs_dir() / (f"run-{run_id}" if run_id is not None else "adhoc")
    return base / f"task-{task_id}" / "events.jsonl"
