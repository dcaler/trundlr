from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.database import get_db
from app.models import Resource, Task
from app.schemas import ResourceCreate, ResourceRead, ResourceUpdate

router = APIRouter(prefix="/api/resources", tags=["resources"])


@router.get("/", response_model=List[ResourceRead])
def list_resources(session: Session = Depends(get_db)):
    return session.exec(select(Resource)).all()


@router.post("/", response_model=ResourceRead, status_code=201)
def create_resource(data: ResourceCreate, session: Session = Depends(get_db)):
    resource = Resource(**data.model_dump())
    session.add(resource)
    session.commit()
    session.refresh(resource)
    return resource


@router.get("/{resource_id}", response_model=ResourceRead)
def get_resource(resource_id: int, session: Session = Depends(get_db)):
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    return resource


@router.patch("/{resource_id}", response_model=ResourceRead)
def update_resource(
    resource_id: int, data: ResourceUpdate, session: Session = Depends(get_db)
):
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(resource, key, value)
    session.add(resource)
    session.commit()
    session.refresh(resource)
    return resource


@router.delete("/{resource_id}", status_code=204)
def delete_resource(resource_id: int, session: Session = Depends(get_db)):
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    # Unassign tasks rather than cascade-delete them
    for task in session.exec(select(Task).where(Task.resource_id == resource_id)).all():
        task.resource_id = None
        session.add(task)
    session.delete(resource)
    session.commit()
