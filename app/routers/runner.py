import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import nullslast
from sqlmodel import Session, select

from app.database import get_db
from app.models import AppSettings, Project, Resource, ResourceKind, Task, TaskResource, TaskStatus
from app.scheduling import conflicting_resource_ids, is_blocked_at, spanning_resource_ids
from app.schemas import RunnerClaimRead
from app.validation import DBId

router = APIRouter(prefix="/api/runner", tags=["runner"])


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


def _next_candidate(session: Session, resource_id: int) -> tuple[Optional[Task], str]:
    """The task a runner for this resource would claim next, or (None, idle reason).

    Walks queued tasks with a command in start_date order, skipping any whose
    dependency isn't done, so unrelated tasks can run past a chain blocked by a
    failed upstream.
    """
    candidates = session.exec(
        select(Task)
        .join(TaskResource, Task.id == TaskResource.task_id)
        .where(TaskResource.resource_id == resource_id)
        .where(Task.status == TaskStatus.todo)
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
        return candidate, idle_reason
    return None, idle_reason


def _draining_for(session: Session, resource_id: int, now: datetime) -> Optional[Task]:
    """A spanning lane's task that this resource must make way for.

    A spanning lane (e.g. GPU 0+1) can only start when every lane it conflicts
    with is idle, which busy single lanes rarely are at once. So once its next
    task is eligible — dependency done, start time reached, lane not blocked —
    the lanes it spans stop claiming until it has run.
    """
    for sid in sorted(spanning_resource_ids(session, resource_id)):
        if _active_task(session, sid) or is_blocked_at(session, sid, now):
            continue
        task, _ = _next_candidate(session, sid)
        if task and (task.start_date is None or task.start_date <= now):
            return task
    return None


# Claims check other lanes before writing, so two runners claiming at once could
# both pass the conflict check. The app runs as a single uvicorn process, so a
# process lock makes check-and-claim atomic.
_claim_lock = threading.Lock()


@router.post("/{resource_id}/claim")
def claim_next_task(resource_id: int = DBId(), session: Session = Depends(get_db)):
    """Atomically claim the next queued task for a cpu/gpu resource.

    Returns the task with project context (200) or 204 if nothing can be claimed,
    with the reason in an X-Runner-Idle header. Sets the task status to
    in_progress and records the actual start time.
    """
    with _claim_lock:
        return _claim(resource_id, session)


def _claim(resource_id: int, session: Session):
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    if resource.kind not in (ResourceKind.cpu, ResourceKind.gpu):
        raise HTTPException(status_code=422, detail="Runner only manages cpu/gpu resources")

    # Don't claim if a task is already running or paused on this resource.
    # The runner is single-threaded: one task at a time regardless of capacity.
    if _active_task(session, resource_id):
        return Response(status_code=204)

    now = _now_naive(session)
    if is_blocked_at(session, resource_id, now):
        return Response(status_code=204, headers={"X-Runner-Idle": "resource-blocked"})

    # Lanes that share hardware (see ResourceSpan) never run at the same time.
    for cid in sorted(conflicting_resource_ids(session, resource_id)):
        busy = _active_task(session, cid)
        if busy:
            return Response(status_code=204,
                            headers={"X-Runner-Idle": f"conflict-busy:{cid}:{busy.id}"})

    waiting = _draining_for(session, resource_id, now)
    if waiting:
        return Response(status_code=204,
                        headers={"X-Runner-Idle": f"draining-for:{waiting.id}"})

    task, idle_reason = _next_candidate(session, resource_id)
    if task is None:
        return Response(status_code=204, headers={"X-Runner-Idle": idle_reason})

    task.status = TaskStatus.in_progress
    if task.start_date and task.end_date:
        dur = task.end_date - task.start_date
        task.end_date = now + dur
    task.start_date = now
    session.add(task)
    session.commit()
    session.refresh(task)

    project = session.get(Project, task.project_id)
    rids = _resource_ids(task.id, session)

    return RunnerClaimRead(
        **task.model_dump(),
        resource_ids=rids,
        project_directory=project.folder if project else None,
    )


@router.post("/{resource_id}/reset-stale")
def reset_stale_tasks(resource_id: int = DBId(), session: Session = Depends(get_db)):
    """Mark in_progress tasks for this resource as failed.

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
        task.status = TaskStatus.failed
        session.add(task)
    session.commit()
    return {"reset": len(stale)}
