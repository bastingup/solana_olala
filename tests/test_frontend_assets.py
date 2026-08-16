"""Frontend integrity: syntax-check every JS module, verify the page's
asset references resolve to real files, and enforce the no-CDN policy."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
NODE = shutil.which("node")

JS_FILES = sorted(FRONTEND.glob("js/*.js"))


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("js_file", JS_FILES, ids=lambda p: p.name)
def test_js_modules_parse(js_file):
    result = subprocess.run(
        [NODE, "--check", str(js_file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_index_references_resolve():
    html = (FRONTEND / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="([^"#]+)"', html)
    for ref in refs:
        if ref.startswith(("http", "data:")):
            continue
        assert (FRONTEND / ref).exists(), f"missing asset: {ref}"


def test_no_cdn_dependencies():
    html = (FRONTEND / "index.html").read_text()
    assert "googleapis.com" not in html
    assert "cdn." not in html
    assert (FRONTEND / "js/vendor/d3.v7.min.js").exists()


def test_fonts_self_hosted():
    fonts_css = FRONTEND / "fonts" / "fonts.css"
    assert fonts_css.exists()
    css = fonts_css.read_text()
    for url in re.findall(r"url\(([^)]+\.woff2)\)", css):
        assert not url.startswith("http"), f"remote font: {url}"
        assert (FRONTEND / "fonts" / url).exists(), f"missing font: {url}"
    # Distinct files per mono weight (regression guard against aliasing).
    mono_files = {url for url in re.findall(r"url\(([^)]+)\)", css)
                  if "redhatmono" in url}
    assert len(mono_files) == 3


def test_stream_event_vocabulary_handled():
    """Every event type the backend publishes has a reducer in the Store."""
    backend = Path(__file__).resolve().parent.parent / "backend" / "olala"
    published = set()
    for py in backend.rglob("*.py"):
        published.update(re.findall(
            r'publish\(\s*"([a-z_]+)"', py.read_text()))
    state_js = (FRONTEND / "js" / "state.js").read_text()
    handled = set(re.findall(r"_on_([a-z_]+)\(", state_js))
    missing = published - handled
    assert not missing, f"backend events with no frontend reducer: {missing}"
