"""The scan banner must actually render — every panel, every state.

A runtime error in one render function silently stops the ones after it:
an edit once left `fill.parentElement` pointing at a removed `const`, so
the bar kept updating while the gear line and source chips below it went
blank. `node --check` passes such code, because it is a ReferenceError,
not a syntax error — only executing it finds the fault.

The harness lives in `tests/js/render_smoke.mjs` and stubs the DOM by
hand; this frontend vendors its libraries on purpose and has no npm
dependencies to borrow a jsdom from.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "js" / "render_smoke.mjs"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; frontend render check "
                           "cannot run")
def test_every_scan_banner_panel_renders():
    result = subprocess.run(["node", str(HARNESS)], capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, (
        f"frontend render check failed\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}")
