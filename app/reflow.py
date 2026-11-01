"""Running the re-flow: one lock, change-only notifications, and the auto re-flow loop.

Every re-flow (task create/delete/finish, the Re-flow button, the hourly timer)
goes through run_reflow so they share SCHEDULE_LOCK with runner claims: a claim
landing mid-re-flow could otherwise have its start time overwritten by the
re-flow's stale copy of the task.
"""

import asyncio
import logging
import threading
import time
from typing import Optional

from sqlmodel import Session

from app.email import send_notification
from app.models import AppSettings, Project, Task
from app.scheduling import reflow_schedule

logger = logging.getLogger(__name__)

SCHEDULE_LOCK = threading.RLock()

# The unschedulable set last emailed about. A re-flow only emails when this
# changes, so an hourly re-flow doesn't resend the same list every hour. Kept in
# memory: a restart may resend the current list once.
_last_notified: Optional[frozenset] = None

AUTO_REFLOW_TICK_SECONDS = 60


def project_names(session: Session, project_ids) -> list[str]:
    """Distinct project names for the given ids, in first-seen order."""
    names = []
    for pid in dict.fromkeys(project_ids):
        proj = session.get(Project, pid)
        names.append(proj.name if proj else f"project {pid}")
    return names


def _notify_unschedulable(session: Session, result: dict) -> None:
    global _last_notified
    # Tasks blocked only by an unschedulable predecessor are noise: the
    # predecessor is already listed.
    items = [t for t in result.get("unscheduled", [])
             if t.get("reason") != "blocked by an unscheduled dependency"]
    key = frozenset((t["id"], t["reason"]) for t in items)
    if key == _last_notified:
        return
    _last_notified = key
    if not items:
        return
    lines = "\n".join(f"  • #{t['id']} {t['title']}: {t['reason']}" for t in items)
    affected = [session.get(Task, t["id"]) for t in items]
    projects = project_names(session, [t.project_id for t in affected if t])
    send_notification(session, "Tasks could not be scheduled",
                      f"{len(items)} task(s) could not be scheduled:\n{lines}",
                      projects=projects)


def run_reflow(session: Session) -> dict:
    """Re-flow, commit, and email if the unschedulable list changed."""
    with SCHEDULE_LOCK:
        result = reflow_schedule(session)
        session.commit()
    _notify_unschedulable(session, result)
    return result


def auto_reflow_tick(engine, last_run: float, now: float) -> float:
    """Re-flow if `auto_reflow_minutes` have passed since `last_run` (monotonic
    seconds). Returns the new last-run time."""
    with Session(engine) as session:
        settings = session.get(AppSettings, 1)
        minutes = settings.auto_reflow_minutes if settings else 60
        if not minutes or now - last_run < minutes * 60:
            return last_run
        result = run_reflow(session)
    logger.info("auto re-flow: %d changed, %d unschedulable",
                result["changed"], len(result["unscheduled"]))
    return now


async def auto_reflow_loop(engine) -> None:
    """Background task started by the app lifespan."""
    last_run = time.monotonic()
    while True:
        await asyncio.sleep(AUTO_REFLOW_TICK_SECONDS)
        try:
            last_run = await asyncio.to_thread(auto_reflow_tick, engine, last_run, time.monotonic())
        except Exception:
            logger.exception("auto re-flow failed")
