from datetime import datetime, timedelta, timezone
from typing import List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.database import get_db
from app.email import send_notification
from app.models import AppSettings, Project, Resource, ResourcePool, Task, TaskResource, TaskStatus
from app.reflow import project_names, run_reflow
from app.schemas import TaskCreate, TaskRead, TaskUpdate
from app.validation import DBId, OptionalDBIdQuery


def _reflow(session: Session) -> None:
    run_reflow(session)


def _notify_status(session: Session, task: Task) -> None:
    # No email for failed: a task only gets there by someone moving it on from
    # error (already emailed) or marking it by hand.
    if task.status not in (TaskStatus.done, TaskStatus.error):
        return
    proj = session.get(Project, task.project_id)
    proj_name = proj.name if proj else f"project {task.project_id}"
    status_str = task.status.value.upper()
    lines = [f"Task: #{task.id} {task.title}", f"Project: {proj_name}", f"Status: {status_str}"]
    if task.duration is not None:
        lines.append(f"Duration: {task.duration:.2f}h")
    if task.exit_code is not None:
        lines.append(f"Exit code: {task.exit_code}")
    if task.log_tail:
        snippet = "\n".join(task.log_tail.splitlines()[-10:])
        lines.append(f"\nLog (last 10 lines):\n{snippet}")
    send_notification(session, f"{status_str}: #{task.id} {task.title}", "\n".join(lines),
                      projects=[proj_name])


def _now_naive(session: Session) -> datetime:
    """Current time in the configured app timezone, as a naive datetime (matching stored task times)."""
    settings = session.get(AppSettings, 1)
    tz = ZoneInfo(settings.timezone if settings else "UTC")
    return datetime.now(tz).replace(tzinfo=None, second=0, microsecond=0)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _return_to_pool(session: Session, task: Task) -> None:
    members = set(session.exec(
        select(ResourcePool.member_id).where(ResourcePool.pool_id == task.pool_id)
    ).all())
    if session.get(Resource, task.pool_id) is not None:
        for tr in session.exec(select(TaskResource).where(TaskResource.task_id == task.id)).all():
            if tr.resource_id in members:
                session.delete(tr)
        session.flush()
        if not session.get(TaskResource, (task.id, task.pool_id)):
            session.add(TaskResource(task_id=task.id, resource_id=task.pool_id))
    task.pool_id = None

# Statuses a task ends in (its times are what actually happened).
FINISHED = (TaskStatus.done, TaskStatus.error, TaskStatus.failed)


def _get_task_or_404(task_id: int, session: Session) -> Task:
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _resource_ids(task_id: int, session: Session) -> list[int]:
    return list(session.exec(
        select(TaskResource.resource_id).where(TaskResource.task_id == task_id)
    ).all())


def _set_resources(task_id: int, resource_ids: list[int], session: Session) -> None:
    for tr in session.exec(select(TaskResource).where(TaskResource.task_id == task_id)).all():
        session.delete(tr)
    for rid in resource_ids:
        session.add(TaskResource(task_id=task_id, resource_id=rid))


def _task_read(task: Task, session: Session) -> TaskRead:
    return TaskRead(**task.model_dump(), resource_ids=_resource_ids(task.id, session))


@router.get("/", response_model=List[TaskRead])
def list_tasks(
    project_id: int | None = OptionalDBIdQuery(),
    resource_id: int | None = OptionalDBIdQuery(),
    session: Session = Depends(get_db),
):
    from sqlalchemy import nullslast
    stmt = select(Task).order_by(nullslast(Task.start_date))
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    if resource_id is not None:
        stmt = stmt.join(TaskResource, Task.id == TaskResource.task_id).where(
            TaskResource.resource_id == resource_id
        )
    tasks = session.exec(stmt).all()

    # Bulk-fetch all task-resource rows to avoid N+1 queries
    task_ids = [t.id for t in tasks]
    tr_rows = (
        session.exec(select(TaskResource).where(TaskResource.task_id.in_(task_ids))).all()
        if task_ids else []
    )
    rid_map: dict[int, list[int]] = {}
    for tr in tr_rows:
        rid_map.setdefault(tr.task_id, []).append(tr.resource_id)

    return [TaskRead(**t.model_dump(), resource_ids=rid_map.get(t.id, [])) for t in tasks]


@router.post("/", response_model=TaskRead, status_code=201)
def create_task(data: TaskCreate, session: Session = Depends(get_db), skip_reflow: bool = Query(False)):
    if not session.get(Project, data.project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    for rid in data.resource_ids:
        if not session.get(Resource, rid):
            raise HTTPException(status_code=404, detail=f"Resource {rid} not found")
    if data.depends_on_id is not None and not session.get(Task, data.depends_on_id):
        raise HTTPException(status_code=404, detail="Dependency task not found")

    task = Task(**data.model_dump(exclude={"resource_ids"}))
    session.add(task)
    session.flush()
    _set_resources(task.id, data.resource_ids, session)
    session.commit()
    if not skip_reflow and data.start_date is None and data.end_date is None:
        _reflow(session)
    session.refresh(task)
    return _task_read(task, session)


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: int = DBId(), session: Session = Depends(get_db)):
    return _task_read(_get_task_or_404(task_id, session), session)


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(
    data: TaskUpdate, task_id: int = DBId(), session: Session = Depends(get_db)
):
    task = _get_task_or_404(task_id, session)
    updates = data.model_dump(exclude_unset=True)

    if "resource_ids" in updates:
        for rid in updates["resource_ids"]:
            if not session.get(Resource, rid):
                raise HTTPException(status_code=404, detail=f"Resource {rid} not found")
        _set_resources(task.id, updates.pop("resource_ids"), session)
        task.pool_id = None  # placed by hand: no longer a pool task to return
    elif updates.get("status") == TaskStatus.todo and task.pool_id is not None:
        # Retrying a task a pool lane took: back onto the pool, for any member.
        _return_to_pool(session, task)

    if "depends_on_id" in updates:
        new_dep = updates["depends_on_id"]
        if new_dep is not None:
            if not session.get(Task, new_dep):
                raise HTTPException(status_code=404, detail="Dependency task not found")
            # Walk the chain from new_dep upward; if we reach task_id it's a cycle.
            cur = new_dep
            visited = set()
            while cur is not None:
                if cur == task_id:
                    raise HTTPException(status_code=422, detail="Setting this dependency would create a cycle")
                if cur in visited:
                    break
                visited.add(cur)
                parent = session.get(Task, cur)
                cur = parent.depends_on_id if parent else None
            task.dependency_broken = False

    if "project_id" in updates:
        if not session.get(Project, updates["project_id"]):
            raise HTTPException(status_code=404, detail="Project not found")

    # Work that fails while running needs attention: it lands in error and stays
    # on the board until someone moves it on to failed. Runners report failed,
    # so this is where that happens (no runner change needed).
    if (updates.get("status") == TaskStatus.failed
            and task.status in (TaskStatus.in_progress, TaskStatus.paused)):
        updates["status"] = TaskStatus.error

    for key, value in updates.items():
        setattr(task, key, value)

    # A runner's result carries the actual end but not the actual start. If the
    # start moved after the claim (e.g. a calendar client saved a stale copy of
    # the event), that end can fall before it; rejecting the result would leave
    # the task in_progress and block its lane, so back the start out of it.
    if (task.status in FINISHED
            and "end_date" in updates and "start_date" not in updates
            and task.start_date and task.end_date and task.end_date < task.start_date):
        task.start_date = task.end_date - timedelta(hours=updates.get("duration") or 0)

    if task.end_date is not None and task.start_date is not None:
        if task.end_date < task.start_date:
            raise HTTPException(
                status_code=422, detail="end_date must not be before start_date"
            )

    session.add(task)
    session.commit()
    if "status" in updates:
        _notify_status(session, task)
        # A task finishing early or late shifts everything planned after it.
        if task.status in FINISHED:
            _reflow(session)
    session.refresh(task)
    return _task_read(task, session)


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: int = DBId(), session: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, session)

    # Any task that depends on this one would dangle (and the FK delete would 500),
    # so clear the link, force it to blocked, and flag it — signalling the user to
    # choose a new dependency rather than silently guessing the predecessor.
    dependents = session.exec(
        select(Task).where(Task.depends_on_id == task_id)
    ).all()
    for dep in dependents:
        dep.depends_on_id = None
        dep.status = TaskStatus.blocked
        dep.dependency_broken = True
        session.add(dep)

    # Resolve names before the delete, while the rows are still loadable.
    blocked_projects = project_names(session, [d.project_id for d in dependents])

    _set_resources(task_id, [], session)
    session.flush()  # clear FK references before deleting the task
    session.delete(task)
    session.commit()
    if dependents:
        titles = ", ".join(d.title for d in dependents)
        send_notification(session, "Tasks blocked — dependency deleted",
                          f"#{task.id} '{task.title}' was deleted.\n\nThe following tasks are now blocked:\n"
                          + "\n".join(f"  • #{d.id} {d.title}" for d in dependents),
                          projects=blocked_projects)
    _reflow(session)


@router.post("/{task_id}/copy", response_model=TaskRead, status_code=201)
def copy_task(task_id: int = DBId(), session: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, session)
    orig_resource_ids = _resource_ids(task_id, session)

    # Start no earlier than now; also no earlier than the last task on each assigned resource.
    candidates: list[datetime] = [_now_naive(session)]
    for rid in orig_resource_ids:
        sibling_ids = session.exec(
            select(TaskResource.task_id).where(TaskResource.resource_id == rid)
        ).all()
        if sibling_ids:
            sibling_tasks = session.exec(select(Task).where(Task.id.in_(sibling_ids))).all()
            times = [
                t.end_date or t.start_date
                for t in sibling_tasks
                if (t.end_date or t.start_date) is not None
            ]
            if times:
                candidates.append(max(times))

    new_start = max(candidates)
    new_end = (new_start + timedelta(hours=task.duration)) if task.duration else None

    new_task = Task(
        title=f"{task.title} (copy)",
        description=task.description,
        command=task.command,
        start_date=new_start,
        end_date=new_end,
        duration=task.duration,
        project_id=task.project_id,
    )
    session.add(new_task)
    session.flush()
    _set_resources(new_task.id, orig_resource_ids, session)
    session.commit()
    _reflow(session)
    session.refresh(new_task)
    return _task_read(new_task, session)
