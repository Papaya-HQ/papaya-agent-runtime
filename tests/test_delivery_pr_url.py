"""The PR URL `ppy deliver` records is the pull request's, never the tool's last line.

Delivering #72 on 2026-09-07 printed `PR: Run gh-axi pr checks 72 -R ... to
monitor CI` and persisted that help line as ``pr_url`` (issue #73): ``gh-axi``
prints a structured record followed by suggestions, and delivery took stdout's
last line on faith. No live GitHub call is made here — the tool is a stub.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from conftest import scale
from papaya_agent_runtime import delivery, repos
from papaya_agent_runtime.delivery import deliver, extract_pr_url
from papaya_agent_runtime.review import record_review
from papaya_agent_runtime.state import init_db
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer

PR = "https://github.com/gizm0duck/papaya-agent-runtime/pull/72"

GH_AXI_CREATE = f"""created{{number,url}}:
  number: 72
  url: {PR}
help[1]:
  Run `gh-axi pr checks 72 -R gizm0duck/papaya-agent-runtime` to monitor CI
"""

GH_ALREADY_EXISTS = f"""a pull request for branch "ppy/task-1-x" into branch "main" already exists:
{PR}
"""


@pytest.fixture
def server(ppy_home):
    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield srv, client
    srv.stop()


@pytest.fixture
def approved_task(server, source_repo) -> int:
    _srv, client = server
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="do work", instructions="")
    deadline = time.monotonic() + scale(15)
    while client.task_status(resp["task_id"])["task"]["status"] != "worker_done":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    record_review(resp["task_id"], "approved")
    return resp["task_id"]


def _stub_tool(monkeypatch, tool: str, responses: dict[str, object]):
    """Answer the tool's `pr <verb>` calls from a table; every other argv runs for real.

    A verb's answer is one ``(rc, stdout, stderr)`` or a list of them, taken in order
    (the last one repeats). `pr list` with no entry finds no open pull request, which
    is what delivery asks before it creates one.
    """
    calls: list[list[str]] = []
    real_run = delivery._run
    asked: dict[str, int] = {}

    def fake_run(argv, cwd=None):
        if argv and argv[0] == tool:
            calls.append(argv)
            verb = argv[2]
            answer = responses.get(verb, (0, "", "") if verb == "list" else None)
            if isinstance(answer, list):
                n = asked.get(verb, 0)
                asked[verb] = n + 1
                answer = answer[min(n, len(answer) - 1)]
            assert answer is not None, f"unexpected {argv}"
            rc, out, err = answer
            return subprocess.CompletedProcess(argv, rc, out, err)
        return real_run(argv, cwd)

    monkeypatch.setattr(delivery, "_pr_tool", lambda: tool)
    monkeypatch.setattr(delivery, "_run", fake_run)
    return calls


def _delivered_event(task_id: int) -> dict:
    row = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'delivered' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        .fetchone()
    )
    return json.loads(row["payload"])


def test_extract_pr_url_finds_the_url_and_nothing_else() -> None:
    assert extract_pr_url(PR + "\n") == PR
    assert extract_pr_url(GH_AXI_CREATE) == PR
    assert extract_pr_url(GH_ALREADY_EXISTS) == PR
    assert extract_pr_url(f"see `{PR}` now") == PR
    assert extract_pr_url("Run `gh-axi pr checks 72` to monitor CI") is None
    assert extract_pr_url("https://github.com/o/r/issues/5") is None
    assert extract_pr_url("") is None
    assert extract_pr_url(None) is None


def test_plain_gh_output_is_the_url_itself(monkeypatch, approved_task) -> None:
    _stub_tool(monkeypatch, "/usr/bin/gh", {"create": (0, PR + "\n", "")})
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.pr_url == PR
    assert result.pr_exists is True
    assert result.note == "pushed; PR opened"
    assert _delivered_event(approved_task)["pr_url"] == PR


def test_structured_output_with_trailing_help_yields_the_url_not_the_help(
    monkeypatch, approved_task
) -> None:
    calls = _stub_tool(monkeypatch, "/x/gh-axi", {"create": (0, GH_AXI_CREATE, "")})
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.pr_url == PR
    assert "monitor CI" not in (result.pr_url or "")
    # The URL was in the output; only the check for an open pull request came first.
    assert [c[2] for c in calls] == ["list", "create"]


def test_success_without_a_readable_url_is_looked_up_by_branch(monkeypatch, approved_task):
    calls = _stub_tool(
        monkeypatch,
        "/usr/bin/gh",
        {
            "create": (0, "Creating pull request...\n", ""),
            "list": [(0, "", ""), (0, PR + "\n", "")],
        },
    )
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.pr_url == PR
    assert result.pr_exists is True
    lookup = [c for c in calls if c[2] == "list"][0]
    assert lookup[3:7] == ["--head", result.branch, "--state", "open"]
    assert "--json" in lookup  # plain gh is asked for machine-readable output


def test_success_and_a_failed_lookup_is_reported_as_created_with_unknown_url(
    monkeypatch, approved_task
) -> None:
    _stub_tool(
        monkeypatch,
        "/x/gh-axi",
        {
            "create": (0, "created but nothing parseable\n", ""),
            "list": [(0, "", ""), (1, "", "boom")],
        },
    )
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.pr_url is None
    assert result.pr_exists is True  # created: retrying creation would be a duplicate
    assert "could not be read" in result.note
    assert "do not create another" in result.note
    event = _delivered_event(approved_task)
    assert event["pr_url"] is None and event["pr_exists"] is True


def test_retry_against_an_existing_pr_records_it_instead_of_failing(monkeypatch, approved_task):
    _stub_tool(monkeypatch, "/usr/bin/gh", {"create": (1, "", GH_ALREADY_EXISTS)})
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.pr_url == PR
    assert result.pr_exists is True
    assert "already open" in result.note


def test_a_genuine_creation_failure_still_says_so(monkeypatch, approved_task) -> None:
    _stub_tool(monkeypatch, "/usr/bin/gh", {"create": (1, "", "GraphQL: something broke")})
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.pr_url is None
    assert result.pr_exists is False
    assert "PR creation failed: GraphQL: something broke" in result.note


# --------------------------------------------------------------------------- #
# PAP-222 (2026-09-17): a second deliver of task 21 said "PR creation failed" with
# no reason, while PR 710 was open for that branch.
# --------------------------------------------------------------------------- #


def test_a_second_deliver_on_the_same_task_updates_the_open_pr_and_says_so(
    monkeypatch, approved_task
) -> None:
    calls = _stub_tool(
        monkeypatch,
        "/usr/bin/gh",
        {
            "list": [(0, "", ""), (0, PR + "\n", "")],
            "create": (0, PR + "\n", ""),
            "edit": (0, "", ""),
        },
    )
    first = deliver(approved_task, push=True, open_pr=True)
    assert first.note == "pushed; PR opened"

    record_review(approved_task, "approved")
    second = deliver(approved_task, push=True, open_pr=True)
    assert second.note == "pushed; PR #72 updated"
    assert second.pr_url == PR and second.pr_exists is True
    verbs = [c[2] for c in calls]
    assert verbs == ["list", "create", "list", "edit"]  # never a second `pr create`
    edit = calls[-1]
    assert edit[3] == "72" and "--body" in edit
    event = _delivered_event(approved_task)
    assert event["pr_updated"] is True and event["pr_error"] is None

    from papaya_agent_runtime import serve

    assert serve.delivery_line(event) == f"PR #72 updated: {PR}"


def test_a_failing_gh_puts_its_stderr_in_the_phase_line_and_the_deficiency(
    monkeypatch, approved_task
) -> None:
    said = (
        "pull request create failed: GraphQL: Resource not accessible by integration "
        "(createPullRequest)\nsecond line of detail"
    )
    _stub_tool(monkeypatch, "/usr/bin/gh", {"create": (1, "", said)})
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.note == f"pushed; PR creation failed: {said}"  # verbatim, not cut

    from papaya_agent_runtime import deficiencies, serve

    event = _delivered_event(approved_task)
    assert event["pr_error"] == said
    line = serve.delivery_line(event)
    assert line == f"Pushed, but the pull request could not be opened: {said}"

    rows = [d for d in deficiencies.ledger(include_all=True) if d.kind == "delivery-failed"]
    assert len(rows) == 1
    assert rows[0].detail == "could not open a pull request"
    assert rows[0].evidence[-1]["error"] == said
    assert rows[0].evidence[-1]["task_id"] == approved_task


def test_a_tool_that_fails_with_nothing_on_stderr_still_gives_a_reason(
    monkeypatch, approved_task
) -> None:
    _stub_tool(monkeypatch, "/x/gh-axi", {"create": (1, "error: already exists\n", "")})
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.note == "pushed; PR creation failed: error: already exists"

    _stub_tool(monkeypatch, "/x/gh-axi", {"create": (4, "", "")})
    record_review(approved_task, "approved")
    result = deliver(approved_task, push=True, open_pr=True)
    assert result.note == "pushed; PR creation failed: exit status 4 with no output"
