"""Every `ppy ...` command the runtime tells a turn or a worker to run must exist.

`manager/launch.py` told every manager turn to link tasks to a work item with
`ppy papaya link`, which has never been a command (runtime issue #114). Each turn
discovered that by being refused, worked around it, and reported it — twice on
2026-09-19 alone, and it would have kept happening on every machine forever. One
sentence was wrong; the lasting fix is that a sentence like it cannot merge.

So this module reads the text the runtime *hands to* a turn or a worker — the turn
prompts, the worker command rules, the manager launch prompt, the worker environment
block, the runtime contract and the README — pulls every `ppy` invocation out of it,
and hands each one to the real `cli.build_parser()`. A command whose subcommand does
not exist, or which names a flag that subcommand does not have, fails with the file
and the line it is written on.

The grammar is deliberately small and explicit (see `to_argv`): documentation shorthand
is expanded rather than parsed, so what reaches argparse is a command a shell could run.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import functools
import io
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest

from papaya_agent_runtime import cli, serve

ROOT = Path(__file__).resolve().parents[1]

#: Every file whose text is handed to a turn, a worker, or a person reading how to
#: drive this runtime. A file here may not name a `ppy` command that does not exist.
CORPUS = (
    "src/papaya_agent_runtime/prompts/answer.md",
    "src/papaya_agent_runtime/prompts/brief.md",
    "src/papaya_agent_runtime/prompts/checkin.md",
    "src/papaya_agent_runtime/prompts/ledger.md",
    "src/papaya_agent_runtime/prompts/reconcile.md",
    "src/papaya_agent_runtime/prompts/review.md",
    "src/papaya_agent_runtime/prompts/__init__.py",
    "src/papaya_agent_runtime/providers/command_rules.py",
    "src/papaya_agent_runtime/manager/launch.py",
    "src/papaya_agent_runtime/environment.py",
    "docs/runtime-contract.md",
    "README.md",
)

#: Commands the launcher answers itself, which `cli.py` deliberately does not know.
#: Each is a real command with a real implementation; it just isn't argparse's.
#: `test_the_launcher_only_commands_are_really_in_the_launcher` holds `bin/ppy` to this.
LAUNCHER_ONLY = {
    ("env", "sync"): "handled by bin/ppy through envsync.py; cli.py has no `env` subcommand",
}

#: Invocations that are written down *because they do not exist* — a sentence naming a
#: command a reader must not reach for. Keyed on (repo-relative path, the command text
#: as this module normalises it); the value says why, so an entry cannot be added to
#: silence a real defect without saying so out loud.
NON_EXAMPLES: dict[tuple[str, str], str] = {
    # (no negative examples in the corpus today; an entry here must name its reason)
}

#: What a documentation placeholder stands in as. `1` satisfies a `type=int` argument
#: and a plain string alike; where it stands in front of a `choices=` argument, the
#: parser says so and `_repairs` swaps in a real choice.
DUMMY = "1"

_PLACEHOLDER_RE = re.compile(r"^[<{].*[>}]$", re.S)
_METAVAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_WORD_RE = re.compile(r"^-{0,2}[a-z][a-z0-9-]*$")
_PREFIXES = ("./bin/ppy", "bin/ppy", "ppy")


@dataclass(frozen=True)
class Invocation:
    """One `ppy` command as some file writes it."""

    path: str
    line: int
    raw: str
    argv: tuple[str, ...]

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def text(self) -> str:
        return " ".join(("ppy", *self.argv))


# ---------------------------------------------------------------------------
# Pulling candidate command text out of the corpus
# ---------------------------------------------------------------------------


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _inline_spans(text: str, base_line: int = 1) -> list[tuple[int, str]]:
    """Every backticked code span, including one wrapped over a line break."""
    found = []
    for match in re.finditer(r"(`+)([^`].*?)\1", text, re.S):
        line = base_line + _line_of(text, match.start()) - 1
        found.append((line, _collapse(match.group(2))))
    return found


def _markdown_spans(text: str) -> list[tuple[int, str]]:
    """Fenced-block lines plus inline code spans, with fenced regions read once."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    outside = list(lines)
    fenced = False
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            outside[index] = ""
            index += 1
            continue
        if fenced:
            line = index + 1
            outside[index] = ""
            content = stripped
            # A command continued onto the next line with a trailing backslash.
            while content.endswith("\\") and index + 1 < len(lines):
                index += 1
                outside[index] = ""
                content = content[:-1].strip() + " " + lines[index].strip()
            found.append((line, _collapse(content)))
        index += 1
    found.extend(_inline_spans("\n".join(outside)))
    return found


def _python_strings(text: str) -> list[tuple[int, int, str]]:
    """Every string literal's full text, with f-string expressions as placeholders.

    Implicitly concatenated literals reach us as one `ast.Constant`, which is what
    makes a command written across a line break in a Python prompt visible here.
    """
    tree = ast.parse(text)
    # A literal implicitly concatenated with an f-string is one `JoinedStr` whose plain
    # pieces `ast.walk` also visits on their own; reading both reports every command twice.
    inside = {
        id(piece)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for piece in node.values
    }
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)) or id(node) in inside:
            continue
        span = (node.lineno, node.end_lineno or node.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((*span, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                else:
                    parts.append("<expr>")
            found.append((*span, "".join(parts)))
    return found


def _locate(lines: list[str], first: int, last: int, span: str) -> int:
    """Which source line of a multi-line literal a code span is written on.

    A 400-line prompt is one `ast.Constant`, and its first line is not a useful place
    to send someone. Shorter and shorter prefixes are tried because a span may start
    at the end of one source line and finish on the next.
    """
    for size in (24, 12, 6):
        needle = span[:size]
        if len(needle) < size:
            continue
        for number in range(first, min(last, len(lines)) + 1):
            if needle in lines[number - 1]:
                return number
    return first


def _python_spans(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for first, last, literal in _python_strings(text):
        for _, span in _inline_spans(literal):
            found.append((_locate(lines, first, last, span), span))
    return found


def _spans(path: str) -> list[tuple[int, str]]:
    text = (ROOT / path).read_text()
    if path.endswith(".py"):
        return _python_spans(text)
    return _markdown_spans(text)


# ---------------------------------------------------------------------------
# The grammar: documentation shorthand -> an argv a shell could run
# ---------------------------------------------------------------------------


def _segments(span: str) -> list[str]:
    """One code span may hold more than one command, joined by a shell operator."""
    parts = [span]
    for operator in ("&&", ";", " | ", " || "):
        parts = [piece for part in parts for piece in part.split(operator)]
    return [part.strip() for part in parts]


def _dummy(token: str) -> str:
    inner = token.strip("<>{}").strip()
    # `<claude|codex>` spells the real choices out: the first one is a literal, not a dummy.
    if "|" in inner:
        return inner.split("|")[0].strip()
    return DUMMY


def _choose(token: str) -> str:
    """One alternative stands for a documented choice: `a|b|c`, `--a|--b`, `--a/--deny`.

    A `/` only separates alternatives between flags — `--brief docs/brief.md` is a path.
    """
    if _PLACEHOLDER_RE.match(token):
        return token
    if token.startswith("--"):
        for separator in ("|", "/"):
            parts = token.split(separator)
            if len(parts) > 1 and all(part.startswith("--") for part in parts):
                return parts[0]
        return token
    if "|" in token:
        return token.split("|")[0]
    return token


def _substitute(token: str) -> str:
    if token in ("...", "…", ""):
        return "1"
    if _PLACEHOLDER_RE.match(token) or _METAVAR_RE.match(token):
        return _dummy(token)
    return token


def to_argv(span: str) -> tuple[str, ...] | None:
    """The argv a `ppy` invocation written as documentation would really run.

    The whole grammar, and nothing beyond it:

    * the command must start `ppy`, `bin/ppy` or `./bin/ppy`, optionally after a `$`,
      and the next word must look like a subcommand or a flag — `ppy  ◂ Ready, …` is a
      transcript of what the runtime *said*, not something anyone is told to run;
    * a `#` comment and trailing prose punctuation are dropped, as a shell would;
    * `[...]` marks an optional part — the brackets go and the contents stay, because
      a documented optional flag is still a flag that has to exist;
    * `a|b|c`, `--a|--b` and `--a/--b` are choices — the first alternative stands in;
    * a placeholder (`<task>`, `<worker task id>`, `{branch}`, `...`, a bare `METAVAR`)
      becomes `1`, which satisfies `type=int` and plain strings alike; `PLACEHOLDER_DUMMIES`
      overrides that by name, and `<claude|codex>` is read as the literal choice it spells.

    Returns None when the span is not a `ppy` invocation at all.
    """
    text = span.strip()
    if text.startswith("$"):
        text = text[1:].strip()
    for prefix in _PREFIXES:
        if text == prefix or text.startswith(prefix + " "):
            rest = text[len(prefix) :]
            break
    else:
        return None
    rest = rest.split(" # ", 1)[0].strip()
    rest = re.sub(r"(?<!\.)[.,:;]+$", "", rest)
    if rest and not _WORD_RE.match(rest.split(maxsplit=1)[0]):
        return None
    # Markdown escapes a `|` inside a table cell; a placeholder may hold whitespace.
    rest = rest.replace("\\|", "|").replace("[", " ").replace("]", " ")
    rest = re.sub(r"<([^<>]*)>", lambda m: "<" + "_".join(m.group(1).split()) + ">", rest)
    try:
        tokens = shlex.split(rest)
    except ValueError:
        # An unbalanced quote is documentation prose, not a command.
        return None
    return tuple(_substitute(_choose(token)) for token in tokens)


def invocations() -> list[Invocation]:
    """Every `ppy` invocation the corpus writes down."""
    found = []
    for path in CORPUS:
        for line, span in _spans(path):
            for segment in _segments(span):
                argv = to_argv(segment)
                if argv is None:
                    continue
                found.append(Invocation(path=path, line=line, raw=segment, argv=argv))
    return found


# ---------------------------------------------------------------------------
# Handing each one to the real parser
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _parser() -> argparse.ArgumentParser:
    """The real parser, built once: `parse_args` never mutates it, and six hundred
    invocations rebuilding it is the whole cost of this module."""
    return cli.build_parser()


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _resolve(parser, argv: tuple[str, ...]):
    """Walk the subparser tree as far as the leading words go.

    Answers the deepest parser reached and whether every word was consumed by it.
    """
    depth = 0
    while depth < len(argv) and not argv[depth].startswith("-"):
        action = _subparsers(parser)
        if action is None or argv[depth] not in action.choices:
            break
        parser = action.choices[argv[depth]]
        depth += 1
    return parser, depth


def _value_for(action) -> str:
    if action.choices:
        return str(next(iter(action.choices)))
    return DUMMY


def _by_name(parser) -> dict[str, object]:
    found: dict[str, object] = {}
    for action in parser._actions:
        for name in (*action.option_strings, action.dest, action.metavar):
            if name:
                found[str(name)] = action
    return found


_REQUIRED_RE = re.compile(r"the following arguments are required: (.*)")
_ONE_OF_RE = re.compile(r"one of the arguments (.*) is required")
_EXPECTED_RE = re.compile(r"argument ([^:]+): expected ")
_CHOICE_RE = re.compile(r"argument ([^:]+): invalid choice: '([^']*)'")


def _repairs(parser, argv: tuple[str, ...], message: str) -> list[tuple[str, ...]]:
    """Ways this refusal is documentation shorthand rather than a dead command.

    Three of them, and no others:

    * a required argument the sentence left to the reader (`ppy track --show` does not
      spell the task id out) gets a dummy appended;
    * a flag named on its own, as a reference to the flag (`ppy dispatch --brief`), gets
      its value inserted after it — the point of the sentence is that the flag exists;
    * a placeholder standing where a `choices=` argument goes (`ppy task set-status <id>
      <status>`) becomes a real choice.

    A subcommand that does not exist, or a flag the named subcommand does not have, has
    no repair and stays a failure.
    """
    names = _by_name(parser)

    def action_for(printed: str):
        for part in printed.strip().split("/"):
            if part in names:
                return names[part]
        return None

    wanted: list[str] = []
    if match := _REQUIRED_RE.search(message):
        wanted = [name.strip() for name in match.group(1).split(",") if name.strip()]
    elif match := _ONE_OF_RE.search(message):
        wanted = [match.group(1).split()[0]]
    if wanted:
        grown = list(argv)
        for name in wanted:
            action = action_for(name)
            if action is None:
                return []
            if action.option_strings:
                grown += [action.option_strings[-1], _value_for(action)]
            else:
                grown.append(_value_for(action))
        return [tuple(grown)]

    if match := _EXPECTED_RE.search(message):
        action = action_for(match.group(1))
        if action is None or not action.option_strings:
            return []
        for index, token in enumerate(argv):
            if token in action.option_strings:
                return [(*argv[: index + 1], _value_for(action), *argv[index + 1 :])]
        return []

    if match := _CHOICE_RE.search(message):
        action, offered = action_for(match.group(1)), match.group(2)
        if action is None or offered != DUMMY:
            # A real word the parser does not know is a dead command, not a placeholder.
            return []
        real = _value_for(action)
        return [
            (*argv[:index], real, *argv[index + 1 :])
            for index, token in enumerate(argv)
            if token == DUMMY
        ]

    return []


def _refuses(argv: tuple[str, ...]) -> str | None:
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            _parser().parse_args(list(argv))
    except SystemExit:
        printed = stderr.getvalue().strip().splitlines()
        return printed[-1] if printed else "the parser refused it"
    return None


def _serve_refuses(argv: tuple[str, ...]) -> str | None:
    """Why `ppy serve`'s own parser refuses these arguments, or None.

    `cli.main` hands `serve` its arguments before the main parser runs, because the
    main parser's `REMAINDER` cannot hold one that starts with a flag: there,
    `ppy serve --working-directory x` reads as refused although `serve` takes it. A
    flag `serve` would only ignore with a warning is still a refusal here, since no
    page should tell a person to type it.
    """
    try:
        _known, unknown = serve._parser().parse_known_args(list(argv))
    except serve._ParseFailed as exc:
        return str(exc)
    return f"unrecognized arguments: {' '.join(unknown)}" if unknown else None


def parse_failure(argv: tuple[str, ...]) -> str | None:
    """Why the real parser refuses this argv, or None when it accepts it."""
    if not argv:
        # A bare `ppy`; there is no command to be wrong about.
        return None
    if any(tuple(argv[: len(key)]) == key for key in LAUNCHER_ONLY):
        return None
    if argv[0] == "serve":
        return _serve_refuses(argv[1:])
    resolved, depth = _resolve(_parser(), argv)
    sub = _subparsers(resolved)
    if depth == len(argv) and sub is not None and sub.required:
        # `ppy todo` and friends: a group named as a group, not as a command.
        return None
    first: str | None = None
    queue, seen = [tuple(argv)], set()
    while queue and len(seen) < 24:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        why = _refuses(current)
        if why is None:
            return None
        if first is None:
            first = why
        queue.extend(_repairs(resolved, current, why))
    return first


def findings(found: list[Invocation] | None = None) -> list[str]:
    """One line per `ppy` invocation in the corpus that the real parser refuses."""
    bad = []
    for inv in invocations() if found is None else found:
        key = (inv.path, inv.text)
        if key in NON_EXAMPLES:
            continue
        reason = parse_failure(inv.argv)
        if reason is not None:
            bad.append(f"{inv.where}: `{inv.raw}` -> {reason}")
    return bad


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_every_ppy_command_the_corpus_names_parses() -> None:
    bad = findings()
    assert not bad, "text names `ppy` commands that do not exist:\n" + "\n".join(bad)


def test_the_corpus_is_really_being_read() -> None:
    """A grammar that quietly stops matching would make the invariant vacuous."""
    found = invocations()
    assert len(found) > 200, len(found)
    by_file = {inv.path for inv in found}
    assert set(CORPUS) - by_file == set(), set(CORPUS) - by_file


def test_serve_is_held_to_its_own_parser() -> None:
    """The flags `serve` takes pass; a flag it does not take is still a dead command."""
    assert parse_failure(("serve", "--working-directory", "$PWD")) is None
    assert parse_failure(("serve", "--supervised", "--harness", "codex")) is None
    assert parse_failure(("serve",)) is None
    assert "--no-such-flag" in (parse_failure(("serve", "--no-such-flag")) or "")


def test_the_launcher_only_commands_are_really_in_the_launcher() -> None:
    launcher = (ROOT / "bin" / "ppy").read_text()
    for key, reason in LAUNCHER_ONLY.items():
        assert " ".join(key) in launcher or key[0] in launcher, (key, reason)


def test_every_non_example_names_a_file_in_the_corpus_and_a_reason() -> None:
    for (path, text), reason in NON_EXAMPLES.items():
        assert path in CORPUS, path
        assert reason.strip(), text


# ---------------------------------------------------------------------------
# The grammar itself, on fixtures rather than on the corpus
# ---------------------------------------------------------------------------


def test_a_command_wrapped_over_a_line_break_is_still_found() -> None:
    text = "Link it with `ppy track <task>\n--record PAP-1` when you pick it up.\n"
    spans = _markdown_spans(text)
    argvs = [to_argv(span) for _, span in spans]
    assert ("track", "1", "--record", "PAP-1") in argvs


def test_a_command_inside_a_fenced_block_is_still_found() -> None:
    text = "before\n\n```bash\nppy dispatch --repo <name> \\\n  --brief brief.md\n```\n\nafter\n"
    argvs = [to_argv(span) for _, span in _markdown_spans(text)]
    assert ("dispatch", "--repo", "1", "--brief", "brief.md") in argvs


def test_a_command_split_across_python_string_literals_is_still_found() -> None:
    source = 'RULE = (\n    "…"\n    "link it with `ppy track <task> "\n    "--record PAP-1`."\n)\n'
    spans = _python_spans(source)
    argvs = [to_argv(span) for _, span in spans]
    assert ("track", "1", "--record", "PAP-1") in argvs


def test_a_command_in_a_long_prompt_is_reported_on_its_own_line() -> None:
    """One `ast.Constant` can be four hundred lines; its first is no place to send anyone."""
    source = 'ROLE = (\n    "a. "\n    "b. "\n    "link it with `ppy track <task_id>`. "\n)\n'
    assert _python_spans(source) == [(4, "ppy track <task_id>")]


def test_an_fstring_placeholder_becomes_a_dummy() -> None:
    source = 'MSG = f"run `ppy task {task_id}` to see it"\n'
    argvs = [to_argv(span) for _, span in _python_spans(source)]
    assert ("task", "1") in argvs


def test_a_str_format_placeholder_becomes_a_dummy() -> None:
    argvs = [to_argv(span) for _, span in _markdown_spans("push `ppy gate run {repo}` now\n")]
    assert ("gate", "run", "1") in argvs


def test_optional_parts_and_choices_are_expanded() -> None:
    argv = to_argv('ppy todo add "..." [--run <id>] [--blocked-on user|review|task:<id>]')
    assert argv == ("todo", "add", "1", "--run", "1", "--blocked-on", "user")


@pytest.mark.parametrize(
    "span",
    [
        "ppy status --team",
        "ppy progress <task_id> --phase plan|implement|test|review|blocked|done --note ...",
        "ppy papaya connect --harness <harness>",
        "ppy gate run [repo] [--task N] [--full]",
        "ppy track <task> --record PAP-214",
    ],
)
def test_real_commands_parse(span: str) -> None:
    argv = to_argv(span)
    assert argv is not None
    assert parse_failure(argv) is None, (span, argv)


def test_a_subcommand_that_does_not_exist_fails() -> None:
    assert parse_failure(("papaya", "link", "1")) is not None


def test_a_flag_from_another_subcommand_fails() -> None:
    """`--team` is `ppy status`'s; naming it on `ppy doctor` is a defect, not a synonym."""
    assert parse_failure(("status", "--team")) is None
    assert parse_failure(("doctor", "--team")) is not None


def test_a_required_positional_is_filled_with_a_dummy_not_reported() -> None:
    assert parse_failure(("track",)) is None


def test_a_group_named_as_a_group_is_not_a_defect() -> None:
    assert parse_failure(("todo",)) is None
    assert parse_failure(("todo", "nonesuch")) is not None


def test_a_launcher_only_command_is_accepted_without_cli_knowing_it() -> None:
    assert _resolve(_parser(), ("env", "sync"))[1] == 0
    assert parse_failure(("env", "sync")) is None
    assert parse_failure(("env", "nonesuch")) is not None


def test_a_negative_example_is_exempt_only_by_an_explicit_entry(monkeypatch) -> None:
    """A sentence saying "there is no `ppy foo`" is still a sentence naming `ppy foo`.

    It is a finding until someone writes down, in `NON_EXAMPLES`, which file it is in
    and why it is there — an exemption nobody can take by accident.
    """
    argv = to_argv("ppy papaya link <task>")
    assert argv == ("papaya", "link", "1")
    inv = Invocation(path=CORPUS[0], line=7, raw="there is no `ppy papaya link`", argv=argv)

    assert findings([inv]), "a negative example is a finding until it is exempted"
    monkeypatch.setitem(NON_EXAMPLES, (inv.path, inv.text), "fixture: named as a dead command")
    assert findings([inv]) == []
