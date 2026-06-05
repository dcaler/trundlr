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


def _on_signal(sig, frame):
    global _shutdown
    print(f"[runner] Signal {sig} — will stop after the current task completes", flush=True)
    _shutdown = True


def _api(base_url: str, method: str, path: str, body=None):
    """Make a JSON API request. Returns parsed response body, or None for 204."""
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
                return None
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"API {method} {path} → HTTP {e.code}: {body_text}") from e


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
    args = parser.parse_args()

    if not args.resource_id:
        print("ERROR: RUNNER_RESOURCE_ID is required (--resource-id or env var)", file=sys.stderr)
        sys.exit(1)

    base_url = args.api_url
    resource_id = args.resource_id
    poll_interval = args.poll_interval
    log_tail_lines = args.log_tail_lines

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    print(
        f"[runner] Starting  resource_id={resource_id}  api={base_url}"
        f"  poll={poll_interval}s  log_tail={log_tail_lines} lines",
        flush=True,
    )

    # Reset tasks left in_progress by a previous crashed run.
    try:
        result = _api(base_url, "POST", f"/runner/{resource_id}/reset-stale")
        if result and result.get("reset"):
            print(f"[runner] Reset {result['reset']} stale task(s) to failed", flush=True)
    except Exception as e:
        print(f"[runner] Warning: reset-stale failed: {e}", flush=True)

    while not _shutdown:
        # ── Claim next task ────────────────────────────────────────────────
        try:
            task = _api(base_url, "POST", f"/runner/{resource_id}/claim")
        except Exception as e:
            print(f"[runner] Claim error: {e} — retrying in {poll_interval}s", flush=True)
            time.sleep(poll_interval)
            continue

        if task is None:
            time.sleep(poll_interval)
            continue

        task_id = task["id"]
        command = task.get("command") or ""
        project_dir = task.get("project_directory") or "."
        log_file = Path(project_dir) / f"task-{task_id}.log"

        print(f"[runner] Task {task_id}: {task['title']!r}", flush=True)

        if not command:
            print(f"[runner] Task {task_id} has no command — marking done", flush=True)
            try:
                _api(base_url, "PATCH", f"/tasks/{task_id}", {"status": "done", "exit_code": 0})
            except Exception as e:
                print(f"[runner] Warning: PATCH failed: {e}", flush=True)
            continue

        # ── Execute ────────────────────────────────────────────────────────
        print(f"[runner] Running: {command!r}  cwd={project_dir!r}  log={log_file}", flush=True)
        actual_start = datetime.now(timezone.utc)
        exit_code = -1

        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
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
            print(f"[runner] Task {task_id} launch error: {e}", flush=True)
            try:
                _api(base_url, "PATCH", f"/tasks/{task_id}", {
                    "status": "failed",
                    "exit_code": -1,
                    "log_tail": str(e),
                })
            except Exception as patch_err:
                print(f"[runner] Warning: PATCH failed: {patch_err}", flush=True)
            continue

        actual_end = datetime.now(timezone.utc)
        duration_h = (actual_end - actual_start).total_seconds() / 3600
        status = "done" if exit_code == 0 else "failed"
        tail = _tail(log_file, log_tail_lines)

        print(
            f"[runner] Task {task_id} → {status}  exit={exit_code}"
            f"  duration={duration_h:.3f}h",
            flush=True,
        )

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
            print(f"[runner] Warning: PATCH failed: {e}", flush=True)

        # No sleep — immediately check for the next task.


if __name__ == "__main__":
    main()
