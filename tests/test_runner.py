"""Runner safety tests.

The runner executes arbitrary shell commands as the invoking user. The
catastrophic failure mode is running a command in an unintended directory
(e.g. a relative grouping label resolved against the launch dir, or an empty
value falling back to "."). ``_resolve_workdir`` is the guard that makes that
impossible: a command is only ever run when the working directory is explicitly
set, absolute, and already exists — the runner never creates it and never falls
back to its launch directory.
"""

import importlib.util
from pathlib import Path

import pytest

# runner.py lives at the repo root, not inside the app package.
_spec = importlib.util.spec_from_file_location(
    "trundlr_runner", Path(__file__).resolve().parent.parent / "runner.py"
)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def test_existing_absolute_dir_is_accepted(tmp_path):
    project_dir, refusal = runner._resolve_workdir(str(tmp_path))
    assert refusal is None
    assert project_dir == str(tmp_path)


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_missing_dir_is_refused(bad):
    project_dir, refusal = runner._resolve_workdir(bad)
    assert project_dir is None
    assert "no working directory" in refusal


@pytest.mark.parametrize("rel", ["experiments", "./build", "../sibling", "Trundlr Dev"])
def test_relative_or_label_dir_is_refused(rel):
    # A grouping label or relative path must never become a cwd.
    project_dir, refusal = runner._resolve_workdir(rel)
    assert project_dir is None
    assert "absolute path" in refusal


def test_absolute_but_nonexistent_dir_is_refused(tmp_path):
    missing = tmp_path / "does-not-exist"
    project_dir, refusal = runner._resolve_workdir(str(missing))
    assert project_dir is None
    assert "does not exist" in refusal


def test_absolute_path_to_a_file_is_refused(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    project_dir, refusal = runner._resolve_workdir(str(f))
    assert project_dir is None
    assert "does not exist" in refusal  # is_dir() is False for a file
