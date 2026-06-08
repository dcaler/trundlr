from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.database import get_db
from app.models import Project, Task, TaskResource
from app.schemas import ProjectCreate, ProjectRead, ProjectUpdate
from app.validation import DBId

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("/", response_model=List[ProjectRead])
def list_projects(archived: bool = False, session: Session = Depends(get_db)):
    return session.exec(select(Project).where(Project.archived == archived).order_by(Project.priority, Project.name)).all()


@router.post("/", response_model=ProjectRead, status_code=201)
def create_project(data: ProjectCreate, session: Session = Depends(get_db)):
    project = Project(**data.model_dump())
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: int = DBId(), session: Session = Depends(get_db)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(
    data: ProjectUpdate,
    project_id: int = DBId(),
    session: Session = Depends(get_db),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int = DBId(), session: Session = Depends(get_db)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    for task in session.exec(select(Task).where(Task.project_id == project_id)).all():
        for tr in session.exec(select(TaskResource).where(TaskResource.task_id == task.id)).all():
            session.delete(tr)
        session.delete(task)
    session.delete(project)
    session.commit()


@router.post("/{project_id}/copy", response_model=ProjectRead, status_code=201)
def copy_project(project_id: int = DBId(), session: Session = Depends(get_db)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    new_project = Project(
        name=f"{project.name} (copy)",
        description=project.description,
        folder=project.folder,
        priority=project.priority,
    )
    session.add(new_project)
    session.flush()
    for task in session.exec(select(Task).where(Task.project_id == project_id)).all():
        new_task = Task(
            title=task.title,
            status=task.status,
            start_date=task.start_date,
            end_date=task.end_date,
            project_id=new_project.id,
        )
        session.add(new_task)
        session.flush()
        for tr in session.exec(select(TaskResource).where(TaskResource.task_id == task.id)).all():
            session.add(TaskResource(task_id=new_task.id, resource_id=tr.resource_id))
    session.commit()
    session.refresh(new_project)
    return new_project


@router.post("/{project_id}/archive", response_model=ProjectRead)
def archive_project(project_id: int = DBId(), session: Session = Depends(get_db)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.archived = True
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.post("/{project_id}/unarchive", response_model=ProjectRead)
def unarchive_project(project_id: int = DBId(), session: Session = Depends(get_db)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.archived = False
    session.add(project)
    session.commit()
    session.refresh(project)
    return project
