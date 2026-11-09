"""Resource page: when weekly windows (or being a pool) replace the default
availability, the page marks those fields as not in use.

Runs the browser view's pure functions under node; skipped if node is absent.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

VIEW = Path(__file__).resolve().parent.parent / "app/static/js/views/resources.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# Load resources.js in a sandbox with the globals it expects from app.js/api.js.
HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [viewPath, expr] = process.argv.slice(1);
const ctx = {
  registerView: () => {},
  api: {},
  escHtml: s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
                 .replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(viewPath, 'utf8'), ctx);
process.stdout.write(JSON.stringify(vm.runInContext(expr, ctx)));
"""


def run(expr: str):
    out = subprocess.run(
        [NODE, "-e", HARNESS, str(VIEW), expr],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


# Cale's case: Mon-Fri 17:00-21:00 windows, simple fields set to 10:00-16:00.
CALE = {"id": 1, "name": "Cale", "kind": "human", "available_from": "10:00",
        "available_to": "16:00", "available_days": 31, "spans_ids": [],
        "member_ids": [], "runner_env": None}
POOL = {"id": 9, "name": "GPU (any)", "kind": "gpu", "available_from": "09:00",
        "available_to": "17:00", "available_days": 31, "spans_ids": [],
        "member_ids": [2, 7], "runner_env": None}


def note(resource, windows):
    return run(f"defaultAvailabilityNote({json.dumps(resource)}, {windows})")


def form(resource, windows):
    return run(f"renderDetailsForm({json.dumps(resource)}, [{json.dumps(resource)}], {windows})")


def test_default_availability_in_use_without_windows():
    assert note(CALE, 0) is None


def test_windows_replace_default_availability():
    assert note(CALE, 5) == "Not in use: replaced by weekly windows (5)"
    assert note(CALE, 1) == "Not in use: replaced by weekly windows (1)"


def test_pool_default_availability_not_in_use():
    assert note(POOL, 0) == "Not in use: a pool has no availability of its own"


def test_form_marks_fields_not_in_use_but_keeps_them():
    html = form(CALE, 5)
    assert "not-in-use" in html
    assert "Not in use: replaced by weekly windows (5)" in html
    # Still visible and editable, with the stored values.
    assert 'name="available_from" value="10:00"' in html
    assert 'name="available_to" value="16:00"' in html


def test_form_without_windows_has_no_marking():
    html = form(CALE, 0)
    assert "not-in-use" not in html
    assert "Not in use" not in html
    assert 'name="available_from" value="10:00"' in html


def test_form_holds_all_editable_fields():
    html = form(CALE, 0)
    for field in ('name="name"', 'name="kind"', 'name="runner_env"',
                  'name="available_from"', 'name="available_to"', 'name="day_0"'):
        assert field in html
