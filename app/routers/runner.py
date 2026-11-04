import logging
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, nullslast
from sqlmodel import Session, select

from app.database import get_db
from app.models import AppSettings, Project, Resource, ResourceKind, Task, TaskResource, TaskStatus
from app.reflow import SCHEDULE_LOCK, run_reflow
from app.scheduling import (
    conflicting_resource_ids, is_blocked_at, pools_of, resource_pools, spanning_resource_ids,
    task_duration,
)
from app.schemas import RunnerClaimRead
from app.validation import DBId

router = APIRouter(prefix="/api/runner", tags=["runner"])
logger = logging.getLogger(__name__)


def _now_naive(session: Session) -> datetime:
    settings = session.get(AppSettings, 1)
    tz = ZoneInfo(settings.timezone if settings else "UTC")
    return datetime.now(tz).replace(tzinfo=None, second=0, microsecond=0)


def _resource_ids(task_id: int, session: Session) -> list[int]:
    return list(session.exec(
        select(TaskResource.resource_id).where(TaskResource.task_id == task_id)
    ).all())


def _active_task(session: Session, resource_id: int) -> Optional[Task]:
    """A task running or paused on the resource, if any."""
    return session.exec(
        select(Task)
        .join(TaskResource, Task.id == TaskResource.task_id)
        .where(TaskResource.resource_id == resource_id)
        .where(Task.status.in_([TaskStatus.in_progress, TaskStatus.paused]))
        .limit(1)
    ).first()


def _next_candidate(
    session: Session, resource_id: int,
    fits: Optional[Callable[[Task], bool]] = None, yield_reason: str = "",
) -> tuple[Optional[Task], str]:
    """The task a runner for this resource would claim next, or (None, idle reason).

    Queued tasks are the resource's own plus those on any pool it is a member
    of. Walks queued tasks with a command in start_date order, skipping any whose
    dependency isn't done, so unrelated tasks can run past a chain blocked by a
    failed upstream. With `fits`, also skips tasks it rejects (idle reason
    `yield_reason`), so shorter work further down the queue can still run.
    """
    queues = [resource_id, *pools_of(session, resource_id)]
    candidates = session.exec(
        select(Task)
        .join(TaskResource, Task.id == TaskResource.task_id)
        .where(TaskResource.resource_id.in_(queues))
        .where(Task.status == TaskStatus.todo)
        .distinct()
        .where(Task.command.isnot(None))
        .where(Task.command != "")
        .order_by(nullslast(Task.start_date))
        .limit(50)
    ).all()

    idle_reason = "empty-queue"
    for candidate in candidates:
        if candidate.depends_on_id is not None:
            dep = session.get(Task, candidate.depends_on_id)
            if dep is None or dep.status != TaskStatus.done:
                if idle_reason == "empty-queue" and dep is not None:
                    idle_reason = f"waiting-dep:{candidate.id}:{dep.id}:{dep.status}"
                continue
        if fits and not fits(candidate):
            idle_reason = yield_reason
            continue
        return candidate, idle_reason
    return None, idle_reason


def _draining_for(session: Session, resource_id: int, now: datetime) -> Optional[Task]:
    """A spanning lane's task that this resource must make way for.

    A spanning lane (e.g. GPU 0+1) can only start when every lane it conflicts
    with is idle, which busy single lanes rarely are at once. So once its next
    task is eligible — dependency done, start time reached, lane not blocked —
    the lanes it spans stop claiming until it has run.

    The drain is sticky: the first time the task comes due its drain_since is
    set, and it keeps draining even if a later re-flow plans it into the future
    again (it would otherwise be bumped indefinitely by whichever lane frees up
    first). drain_since clears when the task is claimed (or by re-flow, if it
    leaves todo or the spanning lane).
    """
    for sid in sorted(spanning_resource_ids(session, resource_id)):
        if _active_task(session, sid) or is_blocked_at(session, sid, now):
            continue
        task, _ = _next_candidate(session, sid)
        if task is None:
            continue
        if task.drain_since is None:
            if task.start_date is not None and task.start_date > now:
                continue
            task.drain_since = now
            session.add(task)
            session.commit()
        return task
    return None


def _upcoming_spanning_task(session: Session, resource_id: int, now: datetime) -> Optional[Task]:
    """The earliest-planned next task of a lane spanning this one, not yet due."""
    upcoming = None
    for sid in spanning_resource_ids(session, resource_id):
        task, _ = _next_candidate(session, sid)
        if (task and task.start_date and task.start_date > now
                and (upcoming is None or task.start_date < upcoming.start_date)):
            upcoming = task
    return upcoming


def _settling(session: Session, resource_id: int, now: datetime) -> Optional[tuple[int, datetime]]:
    """(conflicting lane, settled-at) if a conflicting lane's task ended too recently.

    Ollama keeps a model resident for minutes after a task (2 min on the dual
    lane, 5 on the single lanes), so a lane sharing that card waits for it to
    unload before starting. Same-lane work is never delayed.
    """
    settings = session.get(AppSettings, 1)
    minutes = settings.lane_settle_minutes if settings else 5
    if not minutes:
        return None
    gap = timedelta(minutes=minutes)
    for cid in sorted(conflicting_resource_ids(session, resource_id)):
        last_end = session.exec(
            select(func.max(Task.end_date))
            .join(TaskResource, Task.id == TaskResource.task_id)
            .where(TaskResource.resource_id == cid)
            .where(Task.status.in_([TaskStatus.done, TaskStatus.error, TaskStatus.failed]))
            .where(Task.end_date <= now)  # a future end is a plan, not a finish
        ).one()
        if last_end and last_end + gap > now:
            return cid, last_end + gap
    return None


# Claims check other lanes before writing, so two runners claiming at once could
# both pass the conflict check. The app runs as a single uvicorn process, so a
# process lock makes check-and-claim atomic; it is shared with re-flows, which
# would otherwise overwrite a just-claimed task's start time.


@router.post("/{resource_id}/claim")
def claim_next_task(
    resource_id: int = DBId(),
    after_error: bool = Query(False, description=(
        "Set by a runner whose previous claim got no reply (e.g. timed out). "
        "If that claim did go through, the task it took is handed back.")),
    session: Session = Depends(get_db),
):
    """Atomically claim the next queued task for a cpu/gpu resource.

    Returns the task with project context (200) or 204 if nothing can be claimed,
    with the reason in an X-Runner-Idle header. Sets the task status to
    in_progress and records the actual start time.
    """
    with SCHEDULE_LOCK:
        return _claim(resource_id, session, after_error)


def _claim_read(task: Task, session: Session) -> RunnerClaimRead:
    project = session.get(Project, task.project_id)
    return RunnerClaimRead(
        **task.model_dump(),
        resource_ids=_resource_ids(task.id, session),
        project_directory=project.folder if project else None,
    )


def _claim(resource_id: int, session: Session, after_error: bool = False):
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    if resource.kind not in (ResourceKind.cpu, ResourceKind.gpu):
        raise HTTPException(status_code=422, detail="Runner only manages cpu/gpu resources")
    if resource_id in resource_pools(session):
        raise HTTPException(status_code=422,
                            detail="A pool has no runner: its member lanes' runners claim its tasks")

    # Don't claim if a task is already running or paused on this resource.
    # The runner is single-threaded: one task at a time regardless of capacity.
    active = _active_task(session, resource_id)
    if active:
        # A claim whose reply was lost (the runner timed out while the server
        # committed it) leaves a task in_progress that nothing is running. The
        # runner says so on its next claim; hand the same task back rather than
        # leaving the lane idle behind it forever. (A task the runner finished
        # but could not report also sits in_progress; the runner recognises its
        # own unreported tasks and re-sends the result instead of re-running.)
        if after_error and active.status == TaskStatus.in_progress:
            logger.warning("re-handing task %s to resource %s after a lost claim reply",
                           active.id, resource_id)
            return _claim_read(active, session)
        return Response(status_code=204, headers={"X-Runner-Idle": f"busy:{active.id}"})

    now = _now_naive(session)
    if is_blocked_at(session, resource_id, now):
        return Response(status_code=204, headers={"X-Runner-Idle": "resource-blocked"})

    # Lanes that share hardware (see ResourceSpan) never run at the same time.
    for cid in sorted(conflicting_resource_ids(session, resource_id)):
        busy = _active_task(session, cid)
        if busy:
            return Response(status_code=204,
                            headers={"X-Runner-Idle": f"conflict-busy:{cid}:{busy.id}"})

    settling = _settling(session, resource_id, now)
    if settling:
        cid, until = settling
        return Response(status_code=204,
                        headers={"X-Runner-Idle": f"settling:{cid}:until-{until:%H:%M}"})

    waiting = _draining_for(session, resource_id, now)
    if waiting:
        return Response(status_code=204,
                        headers={"X-Runner-Idle": f"draining-for:{waiting.id}"})

    # Look ahead (as Slurm's backfill does): skip work that would still be
    # running when a spanning lane's planned task is due to start.
    upcoming = _upcoming_spanning_task(session, resource_id, now)
    if upcoming:
        def fits(t: Task) -> bool:
            return now + task_duration(t) <= upcoming.start_date
        task, idle_reason = _next_candidate(session, resource_id, fits, f"yielding-for:{upcoming.id}")
    else:
        task, idle_reason = _next_candidate(session, resource_id)
    if task is None:
        return Response(status_code=204, headers={"X-Runner-Idle": idle_reason})

    task.status = TaskStatus.in_progress
    if task.start_date and task.end_date:
        dur = task.end_date - task.start_date
        task.end_date = now + dur
    task.start_date = now
    task.drain_since = None
    _take_from_pool(session, task, resource_id)
    session.add(task)
    session.commit()
    # A task that starts earlier or later than planned moves everything behind
    # it, including a spanning lane's planned start (which the look-ahead and
    # drain above rely on), so re-plan now rather than at the next event. The
    # claim is already committed: a failed re-flow must not turn it into an
    # error the runner would retry while the task sits in_progress.
    try:
        run_reflow(session)
    except Exception:
        session.rollback()
        logger.exception("re-flow after claiming task %s failed", task.id)
    session.refresh(task)
    return _claim_read(task, session)


def _take_from_pool(session: Session, task: Task, lane_id: int) -> None:
    """Move a claimed pool task onto the lane that claimed it, remembering the
    pool so a reset puts it back (see tasks.update_task)."""
    for tr in session.exec(select(TaskResource).where(TaskResource.task_id == task.id)).all():
        if tr.resource_id in pools_of(session, lane_id):
            task.pool_id = tr.resource_id
            session.delete(tr)
            session.flush()
            if not session.get(TaskResource, (task.id, lane_id)):
                session.add(TaskResource(task_id=task.id, resource_id=lane_id))


@router.post("/{resource_id}/reset-stale")
def reset_stale_tasks(resource_id: int = DBId(), session: Session = Depends(get_db)):
    """Mark in_progress tasks for this resource as error (needs attention).

    Called on runner startup to recover from a crashed previous run.
    """
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    stmt = (
        select(Task)
        .join(TaskResource, Task.id == TaskResource.task_id)
        .where(TaskResource.resource_id == resource_id)
        .where(Task.status == TaskStatus.in_progress)
    )
    stale = session.exec(stmt).all()
    for task in stale:
        task.status = TaskStatus.error
        session.add(task)
    session.commit()
    return {"reset": len(stale)}
