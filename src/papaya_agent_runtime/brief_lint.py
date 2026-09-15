"""Brief lint: the shape a defect brief must have before a worker starts on it.

Across review cycle 4, 25 of 102 worker reflections reported a brief that
stated a cause, mechanism, or current-state fact that was wrong against the
code, and 13 reported scope items that contradicted each other (issue #59).
What made a wrong premise cheap instead of fatal was an expected-discrepancy
table and a plan-note gate; briefs without them lost 15 to 40 minutes per wrong
claim. This module checks that shape. It is a pure function over the brief's
text; ``ppy brief lint`` and ``ppy dispatch --brief`` print what it finds.

A *defect* brief (one with a Hypotheses/Cause section, or one that speaks of a
defect, a FAIL, or a symptom) gets every check:

1. a Symptom section, before any Hypothesis/Cause/Mechanism/Diagnosis section;
2. a probe on every hypothesis — a sub-bullet or trailing clause that starts with
   ``Probe:``, ``Measure:``, ``Reproduce:`` or ``Check:``;
3. an Expected discrepancies table with at least one row per hypothesis;
4. self-consistency: a scope rule saying never/no/must not and a later
   acceptance or test line naming the same backticked token are pointed at;
5. no absolute ``/tmp`` or ``/private/tmp`` path in an evidence section.

Any other brief gets only checks 4 and 5.

Every brief, defect or not, also owes four outcome sections (issue #77): Goals,
Intent, In scope, Out of scope. A brief can specify steps and tests without
saying what success is, why it matters, or where to stop; a worker then
optimises a mechanism or hardens something adjacent while the product outcome
sits unfinished. Check 6 asks only that each section exists and says something
— structure, not semantic quality. The same four sections are what
:func:`standing_scope` carries into every resume and steer packet, so a
continuation is self-contained and cannot silently drop a boundary.

The heuristics are deliberately narrow: a false positive costs the manager's
trust in the lint, a miss costs one more reflection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s*(.+?)\s*#*\s*$")
#: A list item at the left margin (a hypothesis, a scope rule, a discrepancy row).
_TOP_BULLET = re.compile(r"^\s{0,1}(?:[-*+]|\d+[.)])\s+(.*)$")
#: Any list item, however deep (a sub-bullet carrying a probe).
_ANY_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
#: "Probe:" (or the other three) at the start of a line or of a trailing clause.
_PROBE = re.compile(
    r"(?:^|[—–;(]\s*|\.\s+|\s-\s+)[*_]{0,2}(?:probe|measure|reproduce|check)[*_]{0,2}\s*:",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:never|no|must not)\b", re.IGNORECASE)
_BACKTICKED = re.compile(r"`([^`\s]{2,})`")
_TMP_PATH = re.compile(r"(?<![\w./-])(?:/private)?/tmp/[^\s`'\")\]]+")
_DEFECT_WORDS = re.compile(r"\bdefect\b|\bsymptom\b", re.IGNORECASE)
_FAIL_WORD = re.compile(r"\bFAIL\b")
_REVIEW_END = re.compile(
    r"(?:stop|end|finish)\s+(?:at|with|in\s+)?(?:`?--phase\s+|phase\s+)?review\b"
    r"|\bphase-review\s+end\b|\bnever\s+(?:call\s+)?done\b"
    r"|\bdo\s+not\s+call\s+done\b",
    re.IGNORECASE,
)
_DONE_END = re.compile(
    r"(?:finish|end|report)\b[^\n]{0,60}(?:`?--phase\s+done|\bphase\s+done\b)",
    re.IGNORECASE,
)
_DONE_NEGATED = re.compile(r"\b(?:never|refus(?:e|ed)|do\s+not|must\s+not|without)\b", re.I)

_CAUSE_WORDS = ("hypothes", "cause", "mechanism", "diagnos")

#: The outcome sections every brief carries, with what each one is for. The
#: patterns anchor at the start of the heading so "Non-goals" is not "Goals".
OUTCOME_SECTIONS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "Goals",
        re.compile(r"^goals?\b"),
        "the observable outcomes this task must deliver, its acceptance criteria, and the "
        "authoritative project contract or plan when one exists",
    ),
    (
        "Intent",
        re.compile(r"^intent\b"),
        "why the task is needed, who benefits, and the desired outcome as distinct from any "
        "suggested implementation",
    ),
    (
        "In scope",
        re.compile(r"^in[ -]scope\b"),
        "the concrete changes, components and verification the worker is authorised to do",
    ),
    (
        "Out of scope",
        re.compile(r"^(?:out[ -]of[ -]scope|not in scope)\b"),
        "the explicit exclusions and stopping boundaries, including tempting adjacent work",
    ),
)
_SCOPE_WORDS = ("scope", "rule", "constraint", "derivation")
_ACCEPTANCE_WORDS = ("acceptance", "test", "verif", "case")


@dataclass(frozen=True)
class Finding:
    """One thing wrong with a brief, at one line (1-based; 0 means the whole file)."""

    line: int
    message: str

    def render(self, path: str | None = None) -> str:
        where = f"{path}:{self.line}" if path else f"line {self.line}"
        return f"{where}: {self.message}"


@dataclass(frozen=True)
class _Line:
    number: int
    text: str
    #: Lowercased headings enclosing this line, outermost first.
    headings: tuple[str, ...]

    def under(self, words: tuple[str, ...]) -> bool:
        return any(word in heading for heading in self.headings for word in words)


def _classified_lines(text: str) -> list[_Line]:
    """Every line with the heading stack that encloses it.

    A ``###`` under a ``## Scope`` still counts as scope; the stack is what makes
    that so, and it resets when a heading of the same or a higher level arrives.
    """
    stack: list[tuple[int, str]] = []
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        match = _HEADING.match(raw)
        if match is not None:
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip().lower()))
        lines.append(_Line(number, raw, tuple(h for _, h in stack)))
    return lines


def _own_heading(line: _Line) -> str | None:
    match = _HEADING.match(line.text)
    return match.group(2).strip().lower() if match else None


def _first_heading_line(lines: list[_Line], words: tuple[str, ...]) -> _Line | None:
    for line in lines:
        heading = _own_heading(line)
        if heading is not None and any(w in heading for w in words):
            return line
    return None


def is_defect_brief(text: str) -> bool:
    """A brief about something broken: it names a cause section, a defect, a FAIL, or a symptom."""
    lines = _classified_lines(text)
    if _first_heading_line(lines, _CAUSE_WORDS) is not None:
        return True
    return bool(_DEFECT_WORDS.search(text) or _FAIL_WORD.search(text))


def _hypotheses(lines: list[_Line]) -> list[tuple[_Line, list[_Line]]]:
    """Each hypothesis with the lines that belong to it (sub-bullets, continuations).

    A hypothesis is a left-margin list item under a Hypotheses/Cause heading. A
    cause section written as paragraphs instead of a list counts each paragraph
    as one hypothesis, so prose does not slip past the probe check.
    """
    items: list[tuple[_Line, list[_Line]]] = []
    in_cause = [ln for ln in lines if ln.under(_CAUSE_WORDS) and _own_heading(ln) is None]
    has_bullets = any(_TOP_BULLET.match(ln.text) for ln in in_cause)
    current: list[_Line] | None = None
    previous_blank = True
    for line in in_cause:
        blank = not line.text.strip()
        starts = (
            bool(_TOP_BULLET.match(line.text))
            if has_bullets
            else (not blank and previous_blank and not _TABLE_ROW.match(line.text))
        )
        if starts:
            current = []
            items.append((line, current))
        elif current is not None and not blank:
            current.append(line)
        if has_bullets and blank:
            current = None
        previous_blank = blank
    return items


def _has_probe(head: _Line, body: list[_Line]) -> bool:
    candidates = [head.text, *[ln.text for ln in body]]
    for text in candidates:
        bullet = _ANY_BULLET.match(text)
        stripped = bullet.group(1) if bullet else text.strip()
        if _PROBE.search(stripped):
            return True
    return False


def _discrepancy_rows(lines: list[_Line]) -> tuple[_Line | None, int]:
    heading = _first_heading_line(lines, ("discrepanc",))
    if heading is None:
        return None, 0
    rows = 0
    header_seen = False
    for line in lines:
        if line.number <= heading.number or not line.under(("discrepanc",)):
            continue
        if _own_heading(line) is not None:
            continue
        if _TABLE_RULE.match(line.text):
            continue
        if _TABLE_ROW.match(line.text):
            if header_seen:
                rows += 1
            header_seen = True
            continue
        if _TOP_BULLET.match(line.text):
            rows += 1
    return heading, rows


def _check_symptom_first(lines: list[_Line]) -> list[Finding]:
    symptom = _first_heading_line(lines, ("symptom",))
    cause = _first_heading_line(lines, _CAUSE_WORDS)
    if symptom is None:
        at = cause.number if cause is not None else 1
        return [
            Finding(
                at,
                "no Symptom section — open a defect brief with `## Symptom`: the observed "
                "behaviour and the persisted evidence to read, before any cause",
            )
        ]
    if cause is not None and cause.number < symptom.number:
        return [
            Finding(
                cause.number,
                f"`{_own_heading(cause)}` comes before `Symptom` (line {symptom.number}) — "
                "state what was observed before what might explain it",
            )
        ]
    return []


def _check_probes(hypotheses: list[tuple[_Line, list[_Line]]]) -> list[Finding]:
    return [
        Finding(
            head.number,
            "hypothesis has no probe — add a sub-bullet or trailing clause starting "
            '"Probe:", "Measure:", "Reproduce:" or "Check:" naming the query, log filter, '
            "or test that would confirm or refute it",
        )
        for head, body in hypotheses
        if not _has_probe(head, body)
    ]


def _check_discrepancies(
    lines: list[_Line], hypotheses: list[tuple[_Line, list[_Line]]]
) -> list[Finding]:
    heading, rows = _discrepancy_rows(lines)
    wanted = len(hypotheses)
    if heading is None:
        cause = _first_heading_line(lines, _CAUSE_WORDS)
        return [
            Finding(
                cause.number if cause is not None else 1,
                "no Expected discrepancies table — add `## Expected discrepancies` with one "
                f"row per hypothesis ({wanted} here): the claim, and what to do if the probe "
                "comes back negative",
            )
        ]
    if rows < wanted:
        return [
            Finding(
                heading.number,
                f"Expected discrepancies has {rows} row(s) for {wanted} hypothesis(es) — "
                "every hypothesis needs a row saying what to do when it is wrong",
            )
        ]
    return []


def _check_self_consistency(lines: list[_Line]) -> list[Finding]:
    rules: dict[str, _Line] = {}
    for line in lines:
        if not line.under(_SCOPE_WORDS) or _own_heading(line) is not None:
            continue
        if not _NEGATION.search(line.text):
            continue
        for token in _BACKTICKED.findall(line.text):
            rules.setdefault(token, line)
    findings: list[Finding] = []
    reported: set[tuple[str, int]] = set()
    for line in lines:
        if not line.under(_ACCEPTANCE_WORDS) or _own_heading(line) is not None:
            continue
        for token in _BACKTICKED.findall(line.text):
            rule = rules.get(token)
            if rule is None or rule.number >= line.number or (token, line.number) in reported:
                continue
            reported.add((token, line.number))
            findings.append(
                Finding(
                    line.number,
                    f"rule (line {rule.number}) and required case name the same token "
                    f"`{token}`; check they agree",
                )
            )
    return findings


def _check_evidence_paths(lines: list[_Line]) -> list[Finding]:
    findings: list[Finding] = []
    for line in lines:
        if not line.under(("evidence",)):
            continue
        for path in _TMP_PATH.findall(line.text):
            findings.append(
                Finding(
                    line.number,
                    f"evidence points at {path} — a temp directory does not survive the "
                    "session; put evidence inside the worktree and name that path",
                )
            )
    return findings


def _section_body(lines: list[_Line], heading: _Line) -> list[_Line]:
    """The lines under ``heading`` until the next heading of its level or higher."""
    level = len(_HEADING.match(heading.text).group(1))  # type: ignore[union-attr]
    body: list[_Line] = []
    for line in lines:
        if line.number <= heading.number:
            continue
        match = _HEADING.match(line.text)
        if match is not None and len(match.group(1)) <= level:
            break
        body.append(line)
    return body


def _outcome_headings(lines: list[_Line]) -> dict[str, _Line | None]:
    found: dict[str, _Line | None] = {}
    for name, pattern, _purpose in OUTCOME_SECTIONS:
        found[name] = None
        for line in lines:
            heading = _own_heading(line)
            if heading is not None and pattern.match(heading):
                found[name] = line
                break
    return found


def outcome_sections(text: str) -> dict[str, str]:
    """The four outcome sections' bodies, by canonical name, for the ones present and non-empty."""
    lines = _classified_lines(text)
    out: dict[str, str] = {}
    for name, heading in _outcome_headings(lines).items():
        if heading is None:
            continue
        body = "\n".join(ln.text for ln in _section_body(lines, heading)).strip("\n")
        if body.strip():
            out[name] = body
    return out


def standing_scope(text: str) -> str | None:
    """The block a resume or steer packet carries so it stands on its own.

    A continuation replaces the worker's instructions for the turn; without the
    brief's boundaries repeated, a replacement packet silently erases them. The
    four sections are quoted verbatim, framed as unchanged unless the message
    itself says otherwise — a deliberate scope change is then something the
    message states, never something it drops. ``None`` when the brief has none
    of the sections, so a brief from before this rule is passed through as-is.
    """
    sections = outcome_sections(text)
    if not sections:
        return None
    parts = [
        "--- Standing scope, carried from the brief. It is unchanged unless the message "
        "above says otherwise; anything it excludes stays excluded. ---"
    ]
    for name, _pattern, _purpose in OUTCOME_SECTIONS:
        body = sections.get(name)
        if body:
            parts.append(f"## {name}\n{body}")
    return "\n\n".join(parts)


def _check_outcome_sections(lines: list[_Line]) -> list[Finding]:
    findings: list[Finding] = []
    headings = _outcome_headings(lines)
    for name, _pattern, purpose in OUTCOME_SECTIONS:
        heading = headings[name]
        if heading is None:
            findings.append(
                Finding(
                    1,
                    f"no {name} section — add `## {name}`: {purpose}. A worker with no "
                    f"{name.lower()} optimises the mechanism instead of the outcome",
                )
            )
            continue
        body = _section_body(lines, heading)
        if not any(ln.text.strip() and _own_heading(ln) is None for ln in body):
            findings.append(
                Finding(
                    heading.number,
                    f"`## {name}` is empty — say {purpose}",
                )
            )
    return findings


def _check_terminal_phase(lines: list[_Line], ends_at: str) -> list[Finding]:
    """Flag only directive-shaped terminal language that disagrees with dispatch."""
    if ends_at == "done":
        match = next((line for line in lines if _REVIEW_END.search(line.text)), None)
        if match:
            return [
                Finding(
                    match.number,
                    "brief says to stop at review, but dispatch uses `--ends-at done`; "
                    "pass `--ends-at review` or make the brief end at done",
                )
            ]
        return []
    match = next(
        (
            line
            for line in lines
            if _DONE_END.search(line.text) and not _DONE_NEGATED.search(line.text)
        ),
        None,
    )
    if match:
        return [
            Finding(
                match.number,
                "brief says to finish at done, but dispatch uses `--ends-at review`; "
                "pass `--ends-at done` or make the brief stop at review",
            )
        ]
    return []


def lint_brief(text: str, *, ends_at: str = "done") -> list[Finding]:
    """Every finding for ``text``, in line order.

    Empty when the brief has the shape a worker can be right about.
    """
    lines = _classified_lines(text)
    findings: list[Finding] = []
    if is_defect_brief(text):
        hypotheses = _hypotheses(lines)
        findings += _check_symptom_first(lines)
        findings += _check_probes(hypotheses)
        if hypotheses:
            findings += _check_discrepancies(lines, hypotheses)
    findings += _check_outcome_sections(lines)
    findings += _check_self_consistency(lines)
    findings += _check_evidence_paths(lines)
    findings += _check_terminal_phase(lines, ends_at)
    return sorted(findings, key=lambda f: f.line)


def render(findings: list[Finding], path: str | None = None) -> str:
    """The findings as one line each, ready to print."""
    return "\n".join(f.render(path) for f in findings)
