"""Soliciting repositories, and reading one properly once it is registered.

Two invariants worth a test. Discovery must only ever *offer* — it reads the forge,
never the filesystem, and it never proposes something that cannot take a pull
request. Onboarding must extract what a brief actually needs (the verification
command and the commands CI really runs) and say plainly what it could not find,
because a silently missing unknown is the one a worker guesses at.
"""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import solicit

# ── Discovery ───────────────────────────────────────────────────────────────


def _row(name: str, *, owner="acme", pushed="2026-09-01T00:00:00Z", **kwargs) -> dict:
    return {
        "name": name,
        "owner": {"login": owner},
        "url": f"https://github.com/{owner}/{name}",
        "description": kwargs.pop("description", ""),
        "pushedAt": pushed,
        "primaryLanguage": {"name": kwargs.pop("language", "Python")},
        "isArchived": kwargs.pop("archived", False),
        "isFork": kwargs.pop("fork", False),
    }


@pytest.fixture
def forge(monkeypatch):
    """Answer `gh` from a fixture so discovery is hermetic."""
    calls: list[list[str]] = []
    answers: dict[str, object] = {}

    def fake(args: list[str]):
        calls.append(args)
        if args[:2] == ["api", "user"] and "--jq" in args:
            return answers.get("viewer", "someone")
        if args[:2] == ["api", "user/orgs"]:
            return answers.get("orgs", [])
        if args[0] == "repo" and args[1] == "list":
            return answers.get("repos", [])
        return []

    monkeypatch.setattr(solicit, "_gh_json", fake)
    return type("Forge", (), {"calls": calls, "answers": answers})


def test_discovery_offers_the_most_recently_pushed_first(forge, ppy_home) -> None:
    forge.answers["repos"] = [
        _row("old", pushed="2024-01-01T00:00:00Z"),
        _row("fresh", pushed="2026-09-10T00:00:00Z"),
    ]
    found = solicit.candidates(owner="acme")
    assert [c.name for c in found] == ["fresh", "old"]


def test_discovery_never_offers_an_archived_repository(forge, ppy_home) -> None:
    """An archived repo cannot take a pull request, so offering it wastes a dispatch."""
    forge.answers["repos"] = [_row("live"), _row("mothballed", archived=True)]
    assert [c.name for c in solicit.candidates(owner="acme")] == ["live"]


def test_forks_are_skipped_unless_asked_for(forge, ppy_home) -> None:
    forge.answers["repos"] = [_row("mine"), _row("theirs", fork=True)]
    assert [c.name for c in solicit.candidates(owner="acme")] == ["mine"]
    both = solicit.candidates(owner="acme", include_forks=True)
    assert {c.name for c in both} == {"mine", "theirs"}


def test_registered_repositories_drop_out_of_the_offer(forge, ppy_home, monkeypatch) -> None:
    """Offering what the runtime already manages is noise, not initiative."""
    forge.answers["repos"] = [_row("app"), _row("site")]
    monkeypatch.setattr(
        solicit,
        "_without_registered",
        lambda found: [c for c in found if c.name != "app"],
    )
    assert [c.name for c in solicit.candidates(owner="acme")] == ["site"]


def test_registered_repos_are_matched_by_name_and_by_forge_slug(forge, ppy_home) -> None:
    """A repo registered under a different local name is still the same repository."""
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    store.add_repo(
        conn,
        name="frontend",
        origin="https://github.com/acme/site.git",
        local_path="/l",
        default_branch="main",
        base_sha="a",
        forge_url="https://github.com/acme/site",
    )
    forge.answers["repos"] = [_row("app"), _row("site")]
    assert [c.name for c in solicit.candidates(owner="acme")] == ["app"]


def test_owners_are_the_viewer_plus_their_organizations(forge) -> None:
    forge.answers["viewer"] = "shane"
    forge.answers["orgs"] = [{"login": "acme"}, {"login": "acme"}, {"login": "other"}]
    assert solicit.owners() == ["shane", "acme", "other"]


def test_a_candidate_reads_as_a_sentence_not_a_row() -> None:
    """Offers are spoken to a person, so they have to read like something said."""
    candidate = solicit.Candidate(
        name="site",
        owner="acme",
        url="https://github.com/acme/site",
        description="The marketing site",
        language="TypeScript",
        pushed_at="2026-09-10T12:00:00Z",
    )
    sentence = candidate.sentence()
    assert sentence.startswith("acme/site")
    assert "TypeScript" in sentence
    assert "2026-09-10" in sentence
    assert "The marketing site" in sentence


def test_a_missing_gh_is_a_plain_refusal(monkeypatch) -> None:
    monkeypatch.setattr(solicit.shutil, "which", lambda _: None)
    with pytest.raises(solicit.SolicitError, match="GitHub CLI is not installed"):
        solicit.owners()


# ── Onboarding ──────────────────────────────────────────────────────────────


@pytest.fixture
def registered(tmp_path, ppy_home):
    """A registered repository whose base clone is a real directory we can fill."""
    from papaya_agent_runtime.state import init_db, store

    clone = tmp_path / "clone"
    clone.mkdir()
    conn = init_db()
    store.add_repo(
        conn,
        name="app",
        origin="https://github.com/acme/app.git",
        local_path=str(clone),
        default_branch="main",
        base_sha="a" * 40,
    )
    return clone


def test_onboarding_finds_the_node_test_command_and_the_right_runner(registered) -> None:
    (registered / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "build": "tsc", "lint": "eslint ."}}),
        encoding="utf-8",
    )
    (registered / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    report = solicit.inspect("app")
    assert "Node" in report.stacks
    assert report.commands["test"] == "pnpm test"
    assert report.commands["lint"] == "pnpm lint"


def test_onboarding_reads_python_verification_from_the_manifest(registered) -> None:
    (registered / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n[tool.ruff]\nline-length = 100\n",
        encoding="utf-8",
    )
    (registered / "uv.lock").write_text("", encoding="utf-8")
    report = solicit.inspect("app")
    assert report.commands["test"] == "uv run pytest"
    assert report.commands["lint"] == "uv run ruff check ."


def test_onboarding_extracts_the_commands_ci_actually_runs(registered) -> None:
    """A brief's verification suite has to be the gate, not a local approximation."""
    workflows = registered / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - run: uv sync --frozen\n"
        "      - run: uv run pytest -q\n"
        "      - run: uv run ruff check .\n",
        encoding="utf-8",
    )
    report = solicit.inspect("app")
    assert report.ci_workflows == ["ci.yml"]
    assert "uv run pytest -q" in report.ci_commands
    assert "uv run ruff check ." in report.ci_commands


def test_onboarding_names_what_it_could_not_determine(registered) -> None:
    """An unknown that is named gets asked about; a silent one gets guessed at."""
    report = solicit.inspect("app")
    joined = " ".join(report.unknowns)
    assert "no test or build command found" in joined
    assert "no CI workflow commands found" in joined
    assert "conventions are unstated" in joined


def test_onboarding_notices_the_contracts_and_design_reference(registered) -> None:
    (registered / "AGENTS.md").write_text("# how we work\n", encoding="utf-8")
    (registered / "design").mkdir()
    report = solicit.inspect("app")
    assert "AGENTS.md" in report.contracts
    assert "design" in report.design
    notes = solicit.render_notes(report)
    assert "UI work here has a reference to match" in notes


def test_an_unregistered_repo_is_refused_with_the_names_that_do_exist(registered) -> None:
    with pytest.raises(solicit.SolicitError, match="registered: app"):
        solicit.inspect("nope")


def test_a_missing_base_clone_says_to_sync_rather_than_reporting_nothing(
    tmp_path, ppy_home
) -> None:
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    store.add_repo(
        conn,
        name="ghost",
        origin="https://github.com/acme/ghost.git",
        local_path=str(tmp_path / "missing"),
        default_branch="main",
        base_sha="a" * 40,
    )
    report = solicit.inspect("ghost")
    assert any("base clone is missing" in u for u in report.unknowns)


def test_onboarding_writes_notes_and_leaves_hand_written_ones_alone(registered) -> None:
    """Re-onboarding after a repo changes its build must not delete what people wrote."""
    from papaya_agent_runtime import memory

    memory.ensure_memory_layout()
    path = memory.repo_notes_path("app")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Hand-written: the staging database needs a tunnel.\n", encoding="utf-8")

    (registered / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
    _, written = solicit.onboard("app")
    assert written == path
    first = path.read_text(encoding="utf-8")
    assert "Hand-written: the staging database needs a tunnel." in first
    assert "`make test`" in first

    (registered / "Makefile").write_text(
        "test:\n\tpytest\nlint:\n\truff check .\n", encoding="utf-8"
    )
    solicit.onboard("app")
    second = path.read_text(encoding="utf-8")
    assert "Hand-written: the staging database needs a tunnel." in second
    assert "`make lint`" in second
    assert second.count(solicit.NOTES_MARKER) == 1
