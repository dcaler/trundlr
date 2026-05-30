"""Capacity / utilization engine.

The load-bearing rule established in the data model (Step 1.1): `Resource.capacity`
and `Task.load` are both floats in the *same per-day unit*, and `Resource.kind`
only fixes what that unit means (hours/day for humans, parallel slots for cpu/gpu).
So utilization is one formula for every resource kind:

    utilization = committed load / capacity

with no branching on resource type. The unit difference is purely semantic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Optional

from sqlmodel import Session, select

from app.models import Resource, ResourceKind, Task, TaskResource


@dataclass(frozen=True)
class DayUtilization:
    day: date
    committed: float  # summed load of tasks active that day (same unit as capacity)
    capacity: float  # the resource's capacity for that day
    utilization: float  # committed / capacity * 100, a percentage (>100 => over-allocated)


def resource_capacity_on_day(resource: Resource, day: date) -> float:
    """Effective capacity of a resource on a specific day.

    For human resources: derived from the availability window if that day-of-week
    is flagged in available_days; returns 0.0 on unavailable days. For cpu/gpu:
    always returns the stored capacity.
    """
    if resource.kind in (ResourceKind.human, ResourceKind.ai):
        if not resource.available_days or not (resource.available_days & (1 << day.weekday())):
            return 0.0
        if not resource.available_from or not resource.available_to:
            return 0.0
        fh, fm = map(int, resource.available_from.split(":"))
        th, tm = map(int, resource.available_to.split(":"))
        return (th * 60 + tm - fh * 60 - fm) / 60.0
    return resource.capacity or 0.0


def _as_date(d: date | datetime) -> date:
    """Extract the date part whether d is a date or datetime."""
    return d.date() if isinstance(d, datetime) else d


def task_active_on(task: Task, day: date) -> bool:
    """Whether `task` consumes load on `day`.

    Both date endpoints are inclusive. A task contributes load only once it has a
    start date; an open-ended task (start set, end None) is active from its start
    onward. A task with no start_date is unscheduled and never contributes.
    Works with both date and datetime values for start_date/end_date.
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
    """Sum the load of every task active on `day`."""
    return sum((t.load for t in tasks if task_active_on(t, day)), 0.0)


def _days(start: date, end: date) -> Iterator[date]:
    for n in range((end - start).days + 1):
        yield start + timedelta(days=n)


def compute_utilization(
    resource: Resource,
    tasks: Iterable[Task],
    start: date,
    end: date,
) -> list[DayUtilization]:
    """Per-day committed load and utilization for one resource over [start, end].

    `tasks` must already be pre-filtered to those assigned to `resource`.
    The range is inclusive of both endpoints; an inverted range yields [].
    """
    task_list = list(tasks)
    result: list[DayUtilization] = []
    for day in _days(start, end):
        committed = daily_committed_load(task_list, day)
        capacity = resource_capacity_on_day(resource, day)
        if capacity > 0:
            utilization = committed / capacity * 100.0
        else:
            # capacity=0 (e.g. unavailable day) with load scheduled — use sentinel
            # so JSON serialization never sees float("inf")
            utilization = 999.0 if committed > 0 else 0.0
        result.append(
            DayUtilization(day=day, committed=committed, capacity=capacity, utilization=utilization)
        )
    return result


def _tasks_for_resource(session: Session, resource_id: int) -> list[Task]:
    task_ids = session.exec(
        select(TaskResource.task_id).where(TaskResource.resource_id == resource_id)
    ).all()
    return session.exec(select(Task).where(Task.id.in_(task_ids))).all() if task_ids else []


def resource_schedule(
    session: Session, resource_id: int, start: date, end: date
) -> Optional[list[DayUtilization]]:
    resource = session.get(Resource, resource_id)
    if resource is None:
        return None
    return compute_utilization(resource, _tasks_for_resource(session, resource_id), start, end)


@dataclass(frozen=True)
class Conflict:
    day: date
    committed: float  # summed load on the over-allocated day
    capacity: float  # the resource's capacity
    overage: float  # committed - capacity, strictly > 0
    tasks: list[Task]  # tasks active that day that contribute to the overage


def detect_conflicts(
    resource: Resource,
    tasks: Iterable[Task],
    start: date,
    end: date,
) -> list[Conflict]:
    """Days in [start, end] where the resource is over-allocated.

    `tasks` must already be pre-filtered to those assigned to `resource`.
    A day is a conflict only when committed > capacity (full booking is not flagged).
    """
    task_list = list(tasks)
    conflicts: list[Conflict] = []
    for row in compute_utilization(resource, task_list, start, end):
        if row.committed > row.capacity:
            contributing = [t for t in task_list if task_active_on(t, row.day)]
            conflicts.append(
                Conflict(
                    day=row.day,
                    committed=row.committed,
                    capacity=row.capacity,
                    overage=row.committed - row.capacity,
                    tasks=contributing,
                )
            )
    return conflicts


def resource_conflicts(
    session: Session, resource_id: int, start: date, end: date
) -> Optional[list[Conflict]]:
    resource = session.get(Resource, resource_id)
    if resource is None:
        return None
    return detect_conflicts(resource, _tasks_for_resource(session, resource_id), start, end)
