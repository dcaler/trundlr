from datetime import datetime, timedelta, timezone
from typing import List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from icalendar import Calendar, Event
from sqlmodel import Session, select

from app.database import get_db
from app.models import AppSettings, Resource, Task
from app.schemas import ResourceCreate, ResourceRead, ResourceUpdate
from app.validation import DBId

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
def get_resource(resource_id: int = DBId(), session: Session = Depends(get_db)):
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    return resource


@router.patch("/{resource_id}", response_model=ResourceRead)
def update_resource(
    data: ResourceUpdate,
    resource_id: int = DBId(),
    session: Session = Depends(get_db),
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


@router.get("/{resource_id}/next-available")
def get_next_available(resource_id: int = DBId(), session: Session = Depends(get_db)):
    """Return the datetime immediately after the last task on this resource ends."""
    if not session.get(Resource, resource_id):
        raise HTTPException(status_code=404, detail="Resource not found")
    tasks = session.exec(select(Task).where(Task.resource_id == resource_id)).all()
    candidates = [
        t.end_date or t.start_date
        for t in tasks
        if (t.end_date or t.start_date) is not None
    ]
    next_dt = max(candidates) if candidates else None
    return {"next_available": next_dt.isoformat() if next_dt else None}


@router.get("/{resource_id}/calendar.ics")
def get_resource_calendar(resource_id: int = DBId(), session: Session = Depends(get_db)):
    """iCal feed of all scheduled tasks for this resource."""
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    app_settings = session.get(AppSettings, 1)
    tz = ZoneInfo(app_settings.timezone if app_settings else "UTC")

    tasks = session.exec(
        select(Task).where(Task.resource_id == resource_id)
    ).all()

    cal = Calendar()
    cal.add("prodid", f"-//Trundlr//Resource {resource_id}//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", resource.name)
    cal.add("x-wr-timezone", str(tz))

    now = datetime.now(timezone.utc)
    for task in tasks:
        if task.start_date is None:
            continue
        # Treat stored naive datetimes as being in the configured timezone
        start = task.start_date.replace(tzinfo=tz) if not task.start_date.tzinfo else task.start_date.astimezone(tz)
        if task.end_date:
            end = task.end_date.replace(tzinfo=tz) if not task.end_date.tzinfo else task.end_date.astimezone(tz)
        else:
            end = start + timedelta(hours=1)

        ev = Event()
        ev.add("summary", task.title)
        if task.description:
            ev.add("description", task.description)
        ev.add("dtstart", start)
        ev.add("dtend", end)
        ev.add("uid", f"task-{task.id}@trundlr")
        ev.add("dtstamp", now)
        cal.add_component(ev)

    return Response(content=cal.to_ical(), media_type="text/calendar; charset=utf-8")


@router.delete("/{resource_id}", status_code=204)
def delete_resource(resource_id: int = DBId(), session: Session = Depends(get_db)):
    resource = session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    # Unassign tasks rather than cascade-delete them
    for task in session.exec(select(Task).where(Task.resource_id == resource_id)).all():
        task.resource_id = None
        session.add(task)
    session.delete(resource)
    session.commit()
