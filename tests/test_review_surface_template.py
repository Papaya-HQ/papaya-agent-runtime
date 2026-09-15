"""The shipped review-surface default: an Atlassian-style stylesheet and template.

`lavish-review new` must produce one self-contained file — stylesheet inlined, no
CDN, no framework — that renders the same with or without lavish-axi running, in
light and dark. These tests pin that contract so the default cannot quietly regress
into "fetch Tailwind from a CDN".
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "review-surfaces"
SCRIPT = SKILL / "scripts" / "lavish-review"
CSS = SKILL / "assets" / "ads.css"
TEMPLATE = SKILL / "assets" / "surface.html"

FORBIDDEN_REFERENCES = ("tailwind", "daisyui", "cdn.", "http://", "https://", "@import", "url(")


def _run(args, cwd):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, timeout=10
    )


def _block(css: str, selector: str) -> str:
    """Return the body of the first top-level block whose selector matches."""
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
    assert match, f"no block for {selector!r}"
    return match.group(1)


def test_stylesheet_defines_light_and_dark_tokens_without_external_references() -> None:
    css = CSS.read_text()
    light = _block(css, ":root")
    dark_media = _block(css, ':root:not([data-theme="light"])')
    dark_explicit = _block(css, ':root[data-theme="dark"]')

    core = (
        "--ads-color-text:",
        "--ads-surface:",
        "--ads-bg-brand-bold:",
        "--ads-border:",
        "--ads-accent-green-bg:",
    )
    for token in core:
        assert token in light, f"{token} missing from the light palette"
        assert token in dark_media, f"{token} missing from the prefers-color-scheme dark palette"
        assert token in dark_explicit, f"{token} missing from the explicit dark palette"
    assert "@media (prefers-color-scheme: dark)" in css
    assert "--ads-space-200:" in light and "--ads-radius-small:" in light

    lowered = css.lower()
    for needle in FORBIDDEN_REFERENCES:
        assert needle not in lowered, f"stylesheet must be self-contained; found {needle!r}"


@pytest.mark.parametrize(
    "component",
    [
        ".ads-lozenge--success",
        ".ads-btn--primary",
        ".ads-message--warning",
        ".ads-card",
        ".ads-stat__value",
        ".ads-table-wrap",
        ".ads-choice",
        "details.ads-details",
    ],
)
def test_stylesheet_ships_the_review_components(component: str) -> None:
    assert component in CSS.read_text()


def test_template_is_self_contained_and_uses_the_input_pattern() -> None:
    html = TEMPLATE.read_text()
    assert "/*__ADS_CSS__*/" in html and "__TITLE__" in html
    assert "<link" not in html and "<script src" not in html
    lowered = html.lower()
    for needle in ("tailwind", "daisyui", "cdn."):
        assert needle not in lowered
    # One queued prompt per decision, from the form's submit — never from a radio change.
    assert 'data-lavish-question="q1"' in html
    assert "window.lavish.queuePrompt(" in html
    assert "queueKey" in html
    assert 'onchange="' not in lowered
    assert 'class="ads-table-wrap"' in html


def test_new_scaffolds_a_single_portable_file(tmp_path) -> None:
    target = tmp_path / "surfaces" / "plan.html"
    result = _run(["new", str(target), "--title", "Radar <A1> & friends"], str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "scaffolded" in result.stdout

    html = target.read_text()
    css = CSS.read_text().rstrip()
    assert css in html, "stylesheet must be inlined verbatim"
    assert "/*__ADS_CSS__*/" not in html and "__TITLE__" not in html
    assert "<title>Radar &lt;A1&gt; &amp; friends</title>" in html
    assert "<h1>Radar &lt;A1&gt; &amp; friends</h1>" in html
    assert "<link" not in html and "<script src" not in html


def test_new_refuses_to_overwrite(tmp_path) -> None:
    target = tmp_path / "plan.html"
    target.write_text("keep me")
    result = _run(["new", str(target)], str(tmp_path))
    assert result.returncode == 1
    assert "refusing to overwrite" in result.stderr
    assert target.read_text() == "keep me"


def test_new_rejects_unknown_options(tmp_path) -> None:
    result = _run(["new", str(tmp_path / "x.html"), "--theme", "dark"], str(tmp_path))
    assert result.returncode == 2
    assert "unknown option" in result.stderr
