from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.database import get_db
from app.models import Project, Task
from app.schemas import ProjectCreate, ProjectRead, ProjectUpdate

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("/", response_model=List[ProjectRead])
def list_projects(session: Session = Depends(get_db)):
    return session.exec(select(Project)).all()


@router.post("/", response_model=ProjectRead, status_code=201)
def create_project(data: ProjectCreate, session: Session = Depends(get_db)):
    project = Project(**data.model_dump())
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: int, session: Session = Depends(get_db)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int, data: ProjectUpdate, session: Session = Depends(get_db)
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
def delete_project(project_id: int, session: Session = Depends(get_db)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    for task in session.exec(select(Task).where(Task.project_id == project_id)).all():
        session.delete(task)
    session.delete(project)
    session.commit()
