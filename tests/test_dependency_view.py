"""Browser helpers for multiple dependencies (app/static/js/app.js): the
✓/✗ lines on the Projects page and task board, and "hide unmet".

Runs the functions under node; skipped if node is absent.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "app/static/js/app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [path, expr] = process.argv.slice(1);
const ctx = { window: { addEventListener: () => {} }, document: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path, 'utf8'), ctx);
process.stdout.write(JSON.stringify(vm.runInContext(expr, ctx)));
"""

TASKS = {
    1: {"id": 1, "title": "Gather", "status": "done"},
    2: {"id": 2, "title": "Collect", "status": "todo"},
    3: {"id": 3, "title": "Draft", "status": "done"},
    4: {"id": 4, "title": "Review <b>", "status": "in_progress"},
}


def run(expr: str):
    out = subprocess.run([NODE, "-e", HARNESS, str(APP_JS), f"const byId = {json.dumps(TASKS)}; {expr}"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_dep_ids_prefers_list_and_falls_back_to_old_field():
    assert run("depIds({depends_on_ids: [2, 3]})") == [2, 3]
    assert run("depIds({depends_on_id: 5})") == [5]
    assert run("depIds({})") == []


def test_unmet_when_any_dependency_not_done():
    assert run("hasUnmetDeps({depends_on_ids: [1, 3]}, byId)") is False
    assert run("hasUnmetDeps({depends_on_ids: [1, 2]}, byId)") is True
    assert run("hasUnmetDeps({depends_on_ids: [99]}, byId)") is True  # missing counts as unmet
    assert run("hasUnmetDeps({depends_on_ids: []}, byId)") is False


def test_lines_show_two_then_more():
    html = run("depLinesHtml({depends_on_ids: [1, 2, 3, 4]}, byId)")
    assert html.count("✓") == 1 and html.count("✗") == 1  # Gather done, Collect not
    assert "Gather" in html and "Collect" in html
    assert "+2 more" in html
    assert "Draft" in html and "Review &lt;b&gt;" in html  # named in the tooltip, escaped
    assert run("depLinesHtml({depends_on_ids: []}, byId)") == ""
    assert "more" not in run("depLinesHtml({depends_on_ids: [1, 2]}, byId)")
