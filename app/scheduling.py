"""Scheduling engine: 1-task-at-a-time per resource.

All resources have an availability window (available_from/available_to/available_days).
Capacity is 1 slot per available day. A day is a conflict when more than one task is
active on it (committed > capacity).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Optional

from sqlmodel import Session, select

from app.models import Resource, ResourceBlockout, ResourceWindow, Task, TaskResource


@dataclass(frozen=True)
class DayUtilization:
    day: date
    committed: float  # number of tasks active that day
    capacity: float   # 1.0 if resource is available, 0.0 if not
    utilization: float  # committed / capacity * 100 (>100 => over-allocated)


def resource_capacity_on_day(
    resource: Resource,
    day: date,
    windows: "list[ResourceWindow] | None" = None,
    blockouts: "list[ResourceBlockout] | None" = None,
) -> float:
    """1.0 if the resource is available on this day, 0.0 otherwise.

    Full-day blockouts take absolute precedence.  When `windows` is not None,
    it overrides the simple available_days bitmask; an empty list on a given
    day_of_week means capacity 0.  When `windows` is None, the simple
    available_from/available_to/available_days fields are used.
    """
    if blockouts:
        dow = day.weekday()
        for b in blockouts:
            if b.start_date <= day <= b.end_date and b.from_time is None:
                return 0.0
    if windows is not None:
        dow = day.weekday()
        return 1.0 if any(w.day_of_week == dow for w in windows) else 0.0
    if not resource.available_days or not (resource.available_days & (1 << day.weekday())):
        return 0.0
    return 1.0


def _as_date(d: date | datetime) -> date:
    return d.date() if isinstance(d, datetime) else d


def task_active_on(task: Task, day: date) -> bool:
    """Whether the task occupies the resource on this day.

    Both endpoints inclusive. An open-ended task (no end_date) is active from its
    start onward. An unscheduled task (no start_date) never contributes.
    """
    if task.start_date is None:
        return False
    start = _as_date(task.start_date)
    if day < start:
        return False
    if task.end_date is None:
        return True
    return day <= _as_date(task.end_date)


def daily_committed_load(tasks: Iterable[Task], day: date) -> float:
    """Count of tasks active on this day (each active task = 1 unit)."""
    return sum(1 for t in tasks if task_active_on(t, day))


def _days(start: date, end: date) -> Iterator[date]:
    for n in range((end - start).days + 1):
        yield start + timedelta(days=n)


def compute_utilization(
    resource: Resource,
    tasks: Iterable[Task],
    start: date,
    end: date,
    windows: "list[ResourceWindow] | None" = None,
    blockouts: "list[ResourceBlockout] | None" = None,
) -> list[DayUtilization]:
    """Per-day task count and utilization for one resource over [start, end].

    `tasks` must already be pre-filtered to those assigned to `resource`.
    Inverted range yields [].
    """
    task_list = list(tasks)
    result: list[DayUtilization] = []
    for day in _days(start, end):
        committed = daily_committed_load(task_list, day)
        capacity = resource_capacity_on_day(resource, day, windows, blockouts)
        if capacity > 0:
            utilization = committed / capacity * 100.0
        else:
            utilization = 999.0 if committed > 0 else 0.0
        result.append(DayUtilization(day=day, committed=committed, capacity=capacity, utilization=utilization))
    return result


def _tasks_for_resource(session: Session, resource_id: int) -> list[Task]:
    task_ids = session.exec(
        select(TaskResource.task_id).where(TaskResource.resource_id == resource_id)
    ).all()
    return session.exec(select(Task).where(Task.id.in_(task_ids))).all() if task_ids else []


def _windows_for_resource(session: Session, resource_id: int) -> "list[ResourceWindow]":
    return list(session.exec(
        select(ResourceWindow).where(ResourceWindow.resource_id == resource_id)
    ).all())


def _blockouts_for_resource(session: Session, resource_id: int) -> "list[ResourceBlockout]":
    return list(session.exec(
        select(ResourceBlockout).where(ResourceBlockout.resource_id == resource_id)
    ).all())


def resource_schedule(
    session: Session, resource_id: int, start: date, end: date
) -> Optional[list[DayUtilization]]:
    resource = session.get(Resource, resource_id)
    if resource is None:
        return None
    windows = _windows_for_resource(session, resource_id)
    blockouts = _blockouts_for_resource(session, resource_id)
    return compute_utilization(
        resource,
        _tasks_for_resource(session, resource_id),
        start, end,
        windows=windows or None,
        blockouts=blockouts or None,
    )


@dataclass(frozen=True)
class Conflict:
    day: date
    committed: float
    capacity: float
    overage: float
    tasks: list[Task]


def detect_conflicts(
    resource: Resource,
    tasks: Iterable[Task],
    start: date,
    end: date,
    windows: "list[ResourceWindow] | None" = None,
    blockouts: "list[ResourceBlockout] | None" = None,
) -> list[Conflict]:
    """Days in [start, end] where more than one task is active (over-allocated).

    `tasks` must already be pre-filtered to those assigned to `resource`.
    Fully-booked (committed == capacity == 1) is NOT a conflict; only committed > 1.
    Tasks scheduled on unavailable days (capacity == 0) are also flagged.
    """
    task_list = list(tasks)
    conflicts: list[Conflict] = []
    for row in compute_utilization(resource, task_list, start, end, windows, blockouts):
        if row.committed > row.capacity:
            contributing = [t for t in task_list if task_active_on(t, row.day)]
            conflicts.append(Conflict(
                day=row.day,
                committed=row.committed,
                capacity=row.capacity,
                overage=row.committed - row.capacity,
                tasks=contributing,
            ))
    return conflicts


def resource_conflicts(
    session: Session, resource_id: int, start: date, end: date
) -> Optional[list[Conflict]]:
    resource = session.get(Resource, resource_id)
    if resource is None:
        return None
    windows = _windows_for_resource(session, resource_id)
    blockouts = _blockouts_for_resource(session, resource_id)
    return detect_conflicts(
        resource,
        _tasks_for_resource(session, resource_id),
        start, end,
        windows=windows or None,
        blockouts=blockouts or None,
    )
