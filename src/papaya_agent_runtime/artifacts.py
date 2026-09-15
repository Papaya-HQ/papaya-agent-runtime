"""Lavish review artifacts with a local feedback loop.

For a review that benefits from a rich surface, the manager writes a self-
contained HTML document plus a JSON feedback sidecar. The user edits the sidecar
(or a future ``lavish-axi`` integration writes it) and the manager reads back
structured feedback. The loop is fully local and dependency-free; ``lavish-axi``
is preferred when installed but never required.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from papaya_agent_runtime.paths import runs_dir
from papaya_agent_runtime.state import init_db, store


class ArtifactError(Exception):
    pass


@dataclass
class Artifact:
    id: int
    run_id: int
    path: str
    feedback_path: str
    session_state: str


def _artifact_dir(run_id: int) -> Path:
    d = runs_dir() / f"run-{run_id}" / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _render_html(title: str, sections: list[dict], feedback_rel: str) -> str:
    body = []
    for i, sec in enumerate(sections):
        heading = html.escape(str(sec.get("heading", f"Item {i + 1}")))
        content = html.escape(str(sec.get("body", "")))
        opts = "".join(
            f'<label><input type="radio" name="q{i}" value="{html.escape(o)}"> '
            f"{html.escape(o)}</label>"
            for o in sec.get("options", [])
        )
        body.append(
            f"<section><h2>{heading}</h2><pre>{content}</pre>"
            f'<div class="options">{opts}</div></section>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
 body{{font:16px/1.6 -apple-system,system-ui,sans-serif;max-width:60rem;
   margin:2rem auto;padding:0 1rem;color:#111}}
 h1{{border-bottom:2px solid #111;padding-bottom:.3rem}}
 section{{margin:1.5rem 0;padding:1rem;border:1px solid #ddd;border-radius:8px}}
 pre{{white-space:pre-wrap;background:#f6f6f6;padding:.75rem;border-radius:6px}}
 .options label{{display:block;margin:.25rem 0}}
 footer{{color:#666;font-size:.85rem;margin-top:2rem}}
</style></head>
<body>
<h1>{html.escape(title)}</h1>
{"".join(body)}
<footer>Provide structured feedback by editing <code>{html.escape(feedback_rel)}</code>
(set <code>"session_state":"closed"</code> when done).</footer>
</body></html>
"""


def create_artifact(run_id: int, title: str, sections: list[dict]) -> Artifact:
    conn = init_db()
    if store.get_run(conn, run_id) is None:
        raise ArtifactError(f"run {run_id} not found")
    d = _artifact_dir(run_id)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    html_path = d / f"review-{stamp}.html"
    feedback_path = d / f"review-{stamp}.feedback.json"
    html_path.write_text(_render_html(title, sections, feedback_path.name), encoding="utf-8")
    feedback_path.write_text(
        json.dumps({"session_state": "open", "responses": {}, "comments": ""}, indent=2),
        encoding="utf-8",
    )
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO artifacts (run_id, path, session_state, created_at, updated_at) "
        "VALUES (?, ?, 'open', ?, ?)",
        (run_id, str(html_path), now, now),
    )
    conn.commit()
    store.append_event(
        conn,
        kind="review_requested",
        payload={"run_id": run_id, "artifact": str(html_path), "title": title},
        run_id=run_id,
    )
    return Artifact(int(cur.lastrowid), run_id, str(html_path), str(feedback_path), "open")


def read_feedback(artifact_id: int) -> dict:
    """Read the sidecar; if the user closed the session, mark the artifact closed."""
    conn = init_db()
    row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if row is None:
        raise ArtifactError(f"artifact {artifact_id} not found")
    html_p = Path(row["path"])
    feedback_path = html_p.with_name(html_p.stem + ".feedback.json")
    if not feedback_path.exists():
        raise ArtifactError(f"feedback sidecar missing: {feedback_path}")
    data = json.loads(feedback_path.read_text(encoding="utf-8"))
    state = data.get("session_state", "open")
    if state != row["session_state"]:
        conn.execute(
            "UPDATE artifacts SET session_state = ?, updated_at = ? WHERE id = ?",
            (state, datetime.now(UTC).isoformat(), artifact_id),
        )
        conn.commit()
    return {"artifact_id": artifact_id, "session_state": state, "feedback": data}
