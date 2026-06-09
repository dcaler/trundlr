#!/usr/bin/env python3
"""
Trundlr Runner — task execution daemon for cpu/gpu resource queues.

Manages a single resource's task queue: claims the next todo task, runs its
shell command in the project directory, and writes results back via the API.

Usage (env vars):
    RUNNER_RESOURCE_ID=3 RUNNER_API_URL=http://trundlr-host:8251 python3 runner.py

Usage (CLI flags):
    python3 runner.py --resource-id 3 --api-url http://trundlr-host:8251

Environment variables:
    RUNNER_RESOURCE_ID    (required) Resource ID to manage
    RUNNER_API_URL        (default: http://localhost:8251) Trundlr API base URL
    RUNNER_POLL_INTERVAL  (default: 10) Seconds to wait between polls when queue is empty
    RUNNER_LOG_TAIL_LINES (default: 100) Lines of task output to store in the task record
    RUNNER_LOG_DIR        (default: <trundlr dir>/logs) Directory for per-task .log files

Safety: a task command is only ever executed when the project's working
directory is explicitly set, absolute, and already exists on disk. The runner
never creates the working directory and never falls back to its launch
directory, so a missing/relative/label-only value can never cause a command to
run against an unintended location. Per-task logs are written to RUNNER_LOG_DIR,
never inside the working directory.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_shutdown = False


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [runner] {msg}", flush=True)


def _on_signal(sig, frame):
    global _shutdown
    _log(f"Signal {sig} — will stop after the current task completes")
    _shutdown = True


def _api(base_url: str, method: str, path: str, body=None):
    """Make a JSON API request. Returns (parsed body, idle_reason) where idle_reason
    is set when the server returns 204 with an X-Runner-Idle header."""
    url = f"{base_url.rstrip('/')}/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 204:
                return None, resp.headers.get("X-Runner-Idle", "")
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"API {method} {path} → HTTP {e.code}: {body_text}") from e


def _resolve_workdir(raw_dir):
    """Validate a project's working directory for command execution.

    Returns ``(project_dir, None)`` only when ``raw_dir`` is explicitly set,
    absolute, and an existing directory; otherwise ``(None, reason)``. The
    runner never creates the directory and never falls back to its launch
    directory, so a missing/relative/label-only value is refused, not run.
    """
    if not raw_dir or not str(raw_dir).strip():
        return None, "project has no working directory set"
    workdir = Path(str(raw_dir).strip()).expanduser()
    if not workdir.is_absolute():
        return None, f"working directory is not an absolute path: {raw_dir!r}"
    if not workdir.is_dir():
        return None, f"working directory does not exist: {raw_dir!r}"
    return str(workdir), None


def _tail(path: Path, n: int) -> str:
    """Return the last n lines of a file as a single string."""
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Trundlr runner daemon")
    parser.add_argument(
        "--resource-id", type=int,
        default=int(os.environ.get("RUNNER_RESOURCE_ID", 0)),
        help="Resource ID to manage (env: RUNNER_RESOURCE_ID)",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("RUNNER_API_URL", "http://localhost:8251"),
        help="Trundlr base URL (env: RUNNER_API_URL)",
    )
    parser.add_argument(
        "--poll-interval", type=int,
        default=int(os.environ.get("RUNNER_POLL_INTERVAL", 10)),
        help="Seconds between polls when queue is empty (env: RUNNER_POLL_INTERVAL)",
    )
    parser.add_argument(
        "--log-tail-lines", type=int,
        default=int(os.environ.get("RUNNER_LOG_TAIL_LINES", 100)),
        help="Lines of output to store in the task record (env: RUNNER_LOG_TAIL_LINES)",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get(
            "RUNNER_LOG_DIR", str(Path(__file__).resolve().parent / "logs")
        ),
        help="Directory for per-task .log files (env: RUNNER_LOG_DIR)",
    )
    args = parser.parse_args()

    if not args.resource_id:
        print("ERROR: RUNNER_RESOURCE_ID is required (--resource-id or env var)", file=sys.stderr)
        sys.exit(1)

    base_url = args.api_url
    resource_id = args.resource_id
    poll_interval = args.poll_interval
    log_tail_lines = args.log_tail_lines
    log_dir = Path(args.log_dir).expanduser()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Resolve server timezone so timestamps match what the server stores.
    server_tz = timezone.utc
    try:
        settings, _ = _api(base_url, "GET", "/settings")
        if settings and settings.get("timezone"):
            from zoneinfo import ZoneInfo
            server_tz = ZoneInfo(settings["timezone"])
    except Exception as e:
        _log(f"Warning: could not fetch server timezone ({e}); falling back to UTC")

    _log(
        f"Starting  resource_id={resource_id}  api={base_url}"
        f"  tz={server_tz}  poll={poll_interval}s  log_tail={log_tail_lines} lines"
        f"  log_dir={log_dir}"
    )

    # Reset tasks left in_progress by a previous crashed run.
    try:
        result, _ = _api(base_url, "POST", f"/runner/{resource_id}/reset-stale")
        if result and result.get("reset"):
            _log(f"Reset {result['reset']} stale task(s) to failed")
    except Exception as e:
        _log(f"Warning: reset-stale failed: {e}")

    last_idle: str = ""
    while not _shutdown:
        # ── Claim next task ────────────────────────────────────────────────
        try:
            task, idle_reason = _api(base_url, "POST", f"/runner/{resource_id}/claim")
        except Exception as e:
            if str(e) != last_idle:
                _log(f"Claim error: {e} — retrying in {poll_interval}s")
                last_idle = str(e)
            time.sleep(poll_interval)
            continue

        if task is None:
            if idle_reason and idle_reason != last_idle:
                _log(f"Idle: {idle_reason}")
                last_idle = idle_reason
            time.sleep(poll_interval)
            continue

        last_idle = ""

        task_id = task["id"]
        command = task.get("command") or ""
        raw_dir = task.get("project_directory")
        log_file = log_dir / f"task-{task_id}.log"

        _log(f"Task {task_id}: {task['title']!r}")

        if not command:
            _log(f"Task {task_id} has no command — marking done")
            try:
                _api(base_url, "PATCH", f"/tasks/{task_id}", {"status": "done", "exit_code": 0})
            except Exception as e:
                _log(f"Warning: PATCH failed: {e}")
            continue

        # ── Validate the working directory ─────────────────────────────────
        # The command runs as the invoking user via the shell. To make it
        # impossible to ever run against an unintended location, the working
        # directory MUST be explicitly set, absolute, and already exist. We
        # never create it and never fall back to the launch directory ("."),
        # so a missing/relative/label-only value is refused, not executed.
        project_dir, refusal = _resolve_workdir(raw_dir)
        if refusal:
            _log(f"Task {task_id} REFUSED — {refusal}")
            try:
                _api(base_url, "PATCH", f"/tasks/{task_id}", {
                    "status": "failed",
                    "exit_code": -1,
                    "log_tail": (
                        f"Refused to run: {refusal}. Set the project's Directory to an "
                        "existing absolute path on the runner before retrying."
                    ),
                })
            except Exception as e:
                _log(f"Warning: PATCH failed: {e}")
            continue

        # ── Execute ────────────────────────────────────────────────────────
        _log(f"Task {task_id} running: {command!r}  cwd={project_dir!r}  log={log_file}")
        actual_start = datetime.now(server_tz)
        exit_code = -1

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_file, "w") as lf:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=project_dir,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                )
            proc.wait()
            exit_code = proc.returncode
        except Exception as e:
            _log(f"Task {task_id} launch error: {e}")
            try:
                _api(base_url, "PATCH", f"/tasks/{task_id}", {
                    "status": "failed",
                    "exit_code": -1,
                    "log_tail": str(e),
                })
            except Exception as patch_err:
                _log(f"Warning: PATCH failed: {patch_err}")
            continue

        actual_end = datetime.now(server_tz)
        duration_h = (actual_end - actual_start).total_seconds() / 3600
        status = "done" if exit_code == 0 else "failed"
        tail = _tail(log_file, log_tail_lines)

        _log(f"Task {task_id} → {status}  exit={exit_code}  duration={duration_h:.3f}h")

        # ── Write results back ─────────────────────────────────────────────
        try:
            _api(base_url, "PATCH", f"/tasks/{task_id}", {
                "status": status,
                "exit_code": exit_code,
                "end_date": actual_end.strftime("%Y-%m-%dT%H:%M:%S"),
                "duration": round(duration_h, 4),
                "log_tail": tail,
            })
        except Exception as e:
            _log(f"Warning: PATCH failed: {e}")

        # No sleep — immediately check for the next task.


if __name__ == "__main__":
    main()
