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


# ── Result reporting ─────────────────────────────────────────────────────────

def _fake_api(fail_first: int):
    """An _api stand-in that raises on the first `fail_first` calls."""
    calls = []

    def api(base_url, method, path, body=None):
        calls.append(body)
        if len(calls) <= fail_first:
            raise RuntimeError("API PATCH → HTTP 422")
        return None, None

    return api, calls


_RESULT = {
    "status": "failed", "exit_code": 1, "end_date": "2026-10-06T10:00:00",
    "duration": 0.0001, "log_tail": "boom",
}


def test_report_result_sends_full_payload_once(monkeypatch):
    api, calls = _fake_api(fail_first=0)
    monkeypatch.setattr(runner, "_api", api)
    runner._report_result("http://x", 7, _RESULT)
    assert calls == [_RESULT]


def test_report_result_falls_back_to_essential_fields(monkeypatch):
    api, calls = _fake_api(fail_first=1)
    monkeypatch.setattr(runner, "_api", api)
    runner._report_result("http://x", 7, _RESULT)
    assert calls[1] == {"status": "failed", "exit_code": 1, "end_date": "2026-10-06T10:00:00"}


def test_report_result_survives_both_failures(monkeypatch):
    api, calls = _fake_api(fail_first=2)
    monkeypatch.setattr(runner, "_api", api)
    runner._report_result("http://x", 7, _RESULT)  # must not raise
    assert len(calls) == 2


def test_sub_second_duration_is_accepted_by_server_schema():
    from app.schemas import TaskUpdate
    duration = max(round(0.15 / 3600, 4), runner.MIN_DURATION_H)
    assert TaskUpdate(duration=duration).duration > 0


def test_report_result_returns_whether_it_got_through(monkeypatch):
    for fails, ok in ((0, True), (1, True), (2, False)):
        api, _ = _fake_api(fail_first=fails)
        monkeypatch.setattr(runner, "_api", api)
        assert runner._report_result("http://x", 7, _RESULT) is ok


# ── Lost claim replies ───────────────────────────────────────────────────────

def _run_main(monkeypatch, tmp_path, script):
    """Run runner.main() against a scripted _api. `script` maps each call, in
    order, to a return value or an exception; the loop stops once it runs out.
    Returns the (method, path, body) of every call."""
    calls = []
    steps = iter(script)

    def api(base_url, method, path, body=None):
        calls.append((method, path, body))
        if method == "GET" or path.endswith("/reset-stale"):
            return None, None
        if method == "PATCH" and body and set(body) == {"log_tail"}:
            return None, None
        try:
            step = next(steps)
        except StopIteration:
            runner._shutdown = True
            return None, "done"
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(runner, "_api", api)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    monkeypatch.setattr(runner, "_shutdown", False)
    monkeypatch.setattr("sys.argv", ["runner.py", "--resource-id", "3",
                                     "--log-dir", str(tmp_path / "logs")])
    runner.main()
    return calls


def _claims(calls):
    return [p for m, p, _ in calls if m == "POST" and "/claim" in p]


def test_claim_after_a_timeout_asks_for_the_lost_task_back(monkeypatch, tmp_path):
    task = {"id": 1493, "title": "t", "command": "true", "project_directory": str(tmp_path)}
    calls = _run_main(monkeypatch, tmp_path, [
        TimeoutError("timed out"),      # claim: reply lost
        (task, None),                   # claim?after_error: same task handed back
        (None, None),                   # result PATCH
    ])
    claims = _claims(calls)
    assert claims[0] == "/runner/3/claim"
    assert claims[1] == "/runner/3/claim?after_error=true"
    assert claims[2] == "/runner/3/claim"  # flag cleared after a reply
    assert ("PATCH", "/tasks/1493") in [(m, p) for m, p, _ in calls]


def test_unreported_task_is_re_reported_not_re_run(monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    task = {"id": 7, "title": "t", "command": f"echo x >> {marker}",
            "project_directory": str(tmp_path)}
    boom = RuntimeError("API PATCH → HTTP 500")
    calls = _run_main(monkeypatch, tmp_path, [
        (task, None),                   # claim
        boom, boom,                     # both result PATCHes fail
        TimeoutError("timed out"),      # claim: reply lost
        (task, None),                   # after_error hands the same task back
        (None, None),                   # re-sent result gets through
        (None, "busy:7"),               # (not reached once reported)
    ])
    assert marker.read_text() == "x\n"  # ran once only
    patches = [b for m, p, b in calls if m == "PATCH" and p == "/tasks/7" and "status" in b]
    assert len(patches) == 3 and patches[-1]["status"] == "done"


def test_busy_with_an_unreported_task_re_sends_its_result(monkeypatch, tmp_path):
    task = {"id": 7, "title": "t", "command": "true", "project_directory": str(tmp_path)}
    boom = RuntimeError("API PATCH → HTTP 500")
    calls = _run_main(monkeypatch, tmp_path, [
        (task, None), boom, boom,       # ran, report lost
        (None, "busy:7"),               # server still sees it running
        (None, None),                   # re-sent result gets through
    ])
    patches = [b for m, p, b in calls if m == "PATCH" and p == "/tasks/7" and "status" in b]
    assert len(patches) == 3


# ── Unload Ollama before each task (incident 2026-11-10) ─────────────────────

class FakeOllama:
    """In-memory stand-in for Ollama's /api/ps and keep_alive:0 unload.
    `stuck` models ignore the unload; `down` urls don't answer."""

    def __init__(self, loaded, stuck=(), down=()):
        self.loaded = {u: list(ms) for u, ms in loaded.items()}
        self.stuck, self.down = set(stuck), set(down)
        self.events = []

    def ps(self, url, timeout=10):
        if url in self.down:
            raise OSError("connection refused")
        self.events.append(("ps", url))
        return list(self.loaded.get(url, []))

    def unload(self, url, model, timeout=30):
        self.events.append(("unload", url, model))
        if model not in self.stuck:
            self.loaded[url].remove(model)


def _use(monkeypatch, fake):
    monkeypatch.setattr(runner, "_ollama_loaded", fake.ps)
    monkeypatch.setattr(runner, "_ollama_unload", fake.unload)


A, B, DUAL = "http://localhost:11434", "http://localhost:11435", "http://localhost:11436"


def test_unload_empties_every_listed_instance(monkeypatch):
    fake = FakeOllama({A: ["gemma4:31b"], DUAL: ["llama3.3:70b"], B: ["gpt-oss:20b"]})
    _use(monkeypatch, fake)
    notes = []
    assert runner.unload_ollama([A, DUAL], timeout=0, poll=0, note=notes.append) is None
    assert fake.loaded == {A: [], DUAL: [], B: ["gpt-oss:20b"]}  # B not listed: untouched
    assert f"{DUAL}: unloading llama3.3:70b" in notes


def test_unload_reports_a_model_that_will_not_go(monkeypatch):
    fake = FakeOllama({DUAL: ["llama3.3:70b"]}, stuck={"llama3.3:70b"})
    _use(monkeypatch, fake)
    problem = runner.unload_ollama([DUAL], timeout=0, poll=0)
    assert problem == f"card still holds llama3.3:70b on {DUAL} after 0s"


def test_unload_skips_an_instance_that_is_not_running(monkeypatch):
    fake = FakeOllama({A: ["m"]}, down={DUAL})
    _use(monkeypatch, fake)
    notes = []
    assert runner.unload_ollama([A, DUAL], timeout=0, poll=0, note=notes.append) is None
    assert any("not answering" in n for n in notes)


def test_task_starts_only_after_its_cards_are_clear(monkeypatch, tmp_path):
    fake = FakeOllama({A: ["gemma4:31b"], DUAL: ["llama3.3:70b"]})
    _use(monkeypatch, fake)
    marker = tmp_path / "ran"
    task = {"id": 1571, "title": "t", "project_directory": str(tmp_path),
            "command": f"echo started >> {marker}", "unload_urls": [A, DUAL]}
    seen_at_start = {}

    real_popen = runner.subprocess.Popen

    def popen(*a, **kw):
        seen_at_start.update({u: list(ms) for u, ms in fake.loaded.items()})
        return real_popen(*a, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    calls = _run_main(monkeypatch, tmp_path, [(task, None), (None, None)])
    assert marker.read_text() == "started\n"
    assert seen_at_start == {A: [], DUAL: []}
    log = (tmp_path / "logs" / "task-1571.log").read_text()
    assert "unloading llama3.3:70b" in log and "cards clear" in log
    result = [b for m, p, b in calls if m == "PATCH" and p == "/tasks/1571" and "status" in b]
    assert result[-1]["status"] == "done"


def test_task_fails_without_running_if_a_card_will_not_clear(monkeypatch, tmp_path):
    fake = FakeOllama({DUAL: ["llama3.3:70b"]}, stuck={"llama3.3:70b"})
    _use(monkeypatch, fake)
    marker = tmp_path / "ran"
    task = {"id": 1572, "title": "t", "project_directory": str(tmp_path),
            "command": f"touch {marker}", "unload_urls": [DUAL]}
    monkeypatch.setenv("RUNNER_UNLOAD_TIMEOUT", "0")
    calls = _run_main(monkeypatch, tmp_path, [(task, None), (None, None)])
    assert not marker.exists()
    result = [b for m, p, b in calls if m == "PATCH" and p == "/tasks/1572" and "status" in b]
    assert result[-1]["status"] == "failed" and result[-1]["exit_code"] == -1
    assert "card still holds llama3.3:70b" in result[-1]["log_tail"]


def test_no_unload_urls_means_no_ollama_calls(monkeypatch, tmp_path):
    fake = FakeOllama({A: ["m"]})
    _use(monkeypatch, fake)
    task = {"id": 9, "title": "t", "command": "true", "project_directory": str(tmp_path)}
    _run_main(monkeypatch, tmp_path, [(task, None), (None, None)])
    assert fake.events == []
