from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.database import get_db
from app.models import Resource, Task
from app.scheduling import compute_utilization, resource_conflicts, resource_schedule
from app.schemas import ConflictRead, DayUtilizationRead, ResourceScheduleRead
from app.validation import MAX_RANGE_DAYS, DBId

router = APIRouter(tags=["schedule"])


def _require_valid_range(from_date: date, to_date: date) -> None:
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="'from' must not be after 'to'")
    # Cap the window: the engine materialises one row per day, so an unbounded
    # range would let a single request exhaust memory/CPU.
    if (to_date - from_date).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"date range must not exceed {MAX_RANGE_DAYS} days",
        )


@router.get("/api/resources/{resource_id}/schedule", response_model=List[DayUtilizationRead])
def get_resource_schedule(
    resource_id: int = DBId(),
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    session: Session = Depends(get_db),
):
    _require_valid_range(from_date, to_date)
    result = resource_schedule(session, resource_id, from_date, to_date)
    if result is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    return result


@router.get("/api/resources/{resource_id}/conflicts", response_model=List[ConflictRead])
def get_resource_conflicts(
    resource_id: int = DBId(),
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    session: Session = Depends(get_db),
):
    _require_valid_range(from_date, to_date)
    result = resource_conflicts(session, resource_id, from_date, to_date)
    if result is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    return result


@router.get("/api/utilization", response_model=List[ResourceScheduleRead])
def get_utilization(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    session: Session = Depends(get_db),
):
    _require_valid_range(from_date, to_date)
    resources = session.exec(select(Resource)).all()
    all_tasks = session.exec(select(Task)).all()
    return [
        ResourceScheduleRead(
            resource_id=resource.id,
            resource_name=resource.name,
            days=compute_utilization(resource, all_tasks, from_date, to_date),
        )
        for resource in resources
    ]
