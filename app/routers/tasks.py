from datetime import timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.database import get_db
from app.models import Project, Resource, Task
from app.schemas import TaskCreate, TaskRead, TaskUpdate
from app.validation import DBId, OptionalDBIdQuery

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _get_task_or_404(task_id: int, session: Session) -> Task:
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/", response_model=List[TaskRead])
def list_tasks(
    project_id: int | None = OptionalDBIdQuery(), session: Session = Depends(get_db)
):
    stmt = select(Task)
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    return session.exec(stmt).all()


@router.post("/", response_model=TaskRead, status_code=201)
def create_task(data: TaskCreate, session: Session = Depends(get_db)):
    if not session.get(Project, data.project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if data.resource_id is not None and not session.get(Resource, data.resource_id):
        raise HTTPException(status_code=404, detail="Resource not found")
    task = Task(**data.model_dump())
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: int = DBId(), session: Session = Depends(get_db)):
    return _get_task_or_404(task_id, session)


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(
    data: TaskUpdate, task_id: int = DBId(), session: Session = Depends(get_db)
):
    task = _get_task_or_404(task_id, session)

    updates = data.model_dump(exclude_unset=True)

    if "resource_id" in updates and updates["resource_id"] is not None:
        if not session.get(Resource, updates["resource_id"]):
            raise HTTPException(status_code=404, detail="Resource not found")

    for key, value in updates.items():
        setattr(task, key, value)

    # Validate merged date range after applying updates
    if task.end_date is not None and task.start_date is not None:
        if task.end_date < task.start_date:
            raise HTTPException(
                status_code=422, detail="end_date must not be before start_date"
            )

    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: int = DBId(), session: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, session)
    session.delete(task)
    session.commit()


@router.post("/{task_id}/copy", response_model=TaskRead, status_code=201)
def copy_task(task_id: int = DBId(), session: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, session)

    new_start = task.start_date
    new_end = task.end_date

    # Auto-place the copy right after the last task on the same resource
    if task.resource_id is not None:
        resource_tasks = session.exec(
            select(Task).where(Task.resource_id == task.resource_id)
        ).all()
        candidates = [
            t.end_date or t.start_date
            for t in resource_tasks
            if (t.end_date or t.start_date) is not None
        ]
        if candidates:
            latest = max(candidates)
            new_start = latest
            new_end = (latest + timedelta(hours=task.duration)) if task.duration else None

    new_task = Task(
        title=f"{task.title} (copy)",
        start_date=new_start,
        end_date=new_end,
        load=task.load,
        duration=task.duration,
        project_id=task.project_id,
        resource_id=task.resource_id,
    )
    session.add(new_task)
    session.commit()
    session.refresh(new_task)
    return new_task
