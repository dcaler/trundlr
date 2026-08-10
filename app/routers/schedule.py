from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.database import get_db
from app.models import Resource, ResourceBlockout, ResourceCalBlock, ResourceWindow, Task, TaskResource
from app.scheduling import calblock_segments, compute_utilization, reflow_schedule, resource_conflicts, resource_schedule
from app.schemas import ConflictRead, DayUtilizationRead, ReflowResultRead, ResourceScheduleRead
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


@router.post("/api/schedule/reflow", response_model=ReflowResultRead)
def reflow(session: Session = Depends(get_db)):
    """Re-flow all todo tasks: priority-driven, dependency-correct, backfilling.
    Computes new start/end times, applies them, and reports any tasks that could
    not be scheduled (left untouched rather than stamped with a bogus date)."""
    result = reflow_schedule(session)
    session.commit()
    return result


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
    all_tasks = {t.id: t for t in session.exec(select(Task)).all()}
    tr_rows = session.exec(select(TaskResource)).all()

    # Build resource_id → [Task] map
    by_resource: dict[int, list[Task]] = {}
    for tr in tr_rows:
        if tr.task_id in all_tasks:
            by_resource.setdefault(tr.resource_id, []).append(all_tasks[tr.task_id])

    # Batch-fetch windows and blockouts for all resources
    windows_by_resource: dict[int, list[ResourceWindow]] = {}
    for w in session.exec(select(ResourceWindow)).all():
        windows_by_resource.setdefault(w.resource_id, []).append(w)
    blockouts_by_resource: dict[int, list[ResourceBlockout]] = {}
    for b in session.exec(select(ResourceBlockout)).all():
        blockouts_by_resource.setdefault(b.resource_id, []).append(b)
    # CalDAV blocks reduce capacity too — expand each into per-day blockout segments.
    for blk in session.exec(select(ResourceCalBlock)).all():
        blockouts_by_resource.setdefault(blk.resource_id, []).extend(calblock_segments(blk))

    return [
        ResourceScheduleRead(
            resource_id=r.id,
            resource_name=r.name,
            days=compute_utilization(
                r,
                by_resource.get(r.id, []),
                from_date, to_date,
                windows=windows_by_resource.get(r.id) or None,
                blockouts=blockouts_by_resource.get(r.id) or None,
            ),
        )
        for r in resources
    ]
