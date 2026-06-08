from datetime import datetime, timedelta, timezone
from typing import List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from icalendar import Calendar, Event
from sqlmodel import Session, select

from app.database import get_db
from app.models import AppSettings, Project, Resource, ResourceBlockout, ResourceWindow, Task, TaskResource
from app.schemas import BlockoutCreate, BlockoutRead, ResourceCreate, ResourceRead, ResourceUpdate, WindowCreate, WindowRead
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
    task_ids = session.exec(
        select(TaskResource.task_id).where(TaskResource.resource_id == resource_id)
    ).all()
    tasks = session.exec(select(Task).where(Task.id.in_(task_ids))).all() if task_ids else []
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

    task_ids = session.exec(
        select(TaskResource.task_id).where(TaskResource.resource_id == resource_id)
    ).all()
    tasks = session.exec(select(Task).where(Task.id.in_(task_ids))).all() if task_ids else []
    project_ids = {t.project_id for t in tasks}
    project_names = {
        p.id: p.name
        for p in session.exec(select(Project).where(Project.id.in_(project_ids))).all()
    }

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
        project_name = project_names.get(task.project_id, "")
        summary = f"{project_name}: {task.title}" if project_name else task.title
        ev.add("summary", summary)
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
    for tr in session.exec(select(TaskResource).where(TaskResource.resource_id == resource_id)).all():
        session.delete(tr)
    for w in session.exec(select(ResourceWindow).where(ResourceWindow.resource_id == resource_id)).all():
        session.delete(w)
    for b in session.exec(select(ResourceBlockout).where(ResourceBlockout.resource_id == resource_id)).all():
        session.delete(b)
    session.flush()  # send child deletes to DB before removing resource (FK ordering)
    session.delete(resource)
    session.commit()


# ── Windows ───────────────────────────────────────────────────────────────────

@router.get("/{resource_id}/windows", response_model=List[WindowRead])
def list_windows(resource_id: int = DBId(), session: Session = Depends(get_db)):
    if not session.get(Resource, resource_id):
        raise HTTPException(status_code=404, detail="Resource not found")
    return session.exec(
        select(ResourceWindow).where(ResourceWindow.resource_id == resource_id)
    ).all()


@router.post("/{resource_id}/windows", response_model=WindowRead, status_code=201)
def create_window(
    data: WindowCreate,
    resource_id: int = DBId(),
    session: Session = Depends(get_db),
):
    if not session.get(Resource, resource_id):
        raise HTTPException(status_code=404, detail="Resource not found")
    window = ResourceWindow(resource_id=resource_id, **data.model_dump())
    session.add(window)
    session.commit()
    session.refresh(window)
    return window


@router.delete("/{resource_id}/windows/{window_id}", status_code=204)
def delete_window(
    resource_id: int = DBId(),
    window_id: int = DBId(),
    session: Session = Depends(get_db),
):
    window = session.get(ResourceWindow, window_id)
    if not window or window.resource_id != resource_id:
        raise HTTPException(status_code=404, detail="Window not found")
    session.delete(window)
    session.commit()


# ── Blockouts ─────────────────────────────────────────────────────────────────

@router.get("/{resource_id}/blockouts", response_model=List[BlockoutRead])
def list_blockouts(resource_id: int = DBId(), session: Session = Depends(get_db)):
    if not session.get(Resource, resource_id):
        raise HTTPException(status_code=404, detail="Resource not found")
    return session.exec(
        select(ResourceBlockout).where(ResourceBlockout.resource_id == resource_id)
    ).all()


@router.post("/{resource_id}/blockouts", response_model=BlockoutRead, status_code=201)
def create_blockout(
    data: BlockoutCreate,
    resource_id: int = DBId(),
    session: Session = Depends(get_db),
):
    if not session.get(Resource, resource_id):
        raise HTTPException(status_code=404, detail="Resource not found")
    blockout = ResourceBlockout(resource_id=resource_id, **data.model_dump())
    session.add(blockout)
    session.commit()
    session.refresh(blockout)
    return blockout


@router.delete("/{resource_id}/blockouts/{blockout_id}", status_code=204)
def delete_blockout(
    resource_id: int = DBId(),
    blockout_id: int = DBId(),
    session: Session = Depends(get_db),
):
    blockout = session.get(ResourceBlockout, blockout_id)
    if not blockout or blockout.resource_id != resource_id:
        raise HTTPException(status_code=404, detail="Blockout not found")
    session.delete(blockout)
    session.commit()
