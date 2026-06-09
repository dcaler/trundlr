from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import nullslast
from sqlmodel import Session, select

from app.database import get_db
from app.models import AppSettings, Project, Resource, ResourceKind, Task, TaskResource, TaskStatus
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


@router.post("/{resource_id}/claim")
def claim_next_task(resource_id: int = DBId(), session: Session = Depends(get_db)):
    """Atomically claim the next queued task for a cpu/gpu resource.

    Returns the task with project context (200) or 204 if the queue is empty.
    Sets the task status to in_progress and records the actual start time.
    """
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    if resource.kind not in (ResourceKind.cpu, ResourceKind.gpu):
        raise HTTPException(status_code=422, detail="Runner only manages cpu/gpu resources")

    # Don't claim if a task is already running on this resource.
    # The runner is single-threaded: one task at a time regardless of capacity.
    already_running = session.exec(
        select(Task)
        .join(TaskResource, Task.id == TaskResource.task_id)
        .where(TaskResource.resource_id == resource_id)
        .where(Task.status == TaskStatus.in_progress)
        .limit(1)
    ).first()
    if already_running:
        return Response(status_code=204)

    task = session.exec(
        select(Task)
        .join(TaskResource, Task.id == TaskResource.task_id)
        .where(TaskResource.resource_id == resource_id)
        .where(Task.status == TaskStatus.todo)
        .where(Task.command.isnot(None))
        .where(Task.command != "")
        .order_by(nullslast(Task.start_date))
        .limit(1)
    ).first()

    if task is None:
        return Response(status_code=204, headers={"X-Runner-Idle": "empty-queue"})

    # If the next task's dependency isn't done yet, wait — never skip ahead.
    if task.depends_on_id is not None:
        dep = session.get(Task, task.depends_on_id)
        if dep is not None and dep.status != TaskStatus.done:
            return Response(
                status_code=204,
                headers={"X-Runner-Idle": f"waiting-dep:{task.id}:{dep.id}:{dep.status}"},
            )

    now = _now_naive(session)
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
