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
from datetime import date, timedelta
from typing import Iterable, Iterator, Optional

from sqlmodel import Session, select

from app.models import Resource, Task


@dataclass(frozen=True)
class DayUtilization:
    day: date
    committed: float  # summed load of tasks active that day (same unit as capacity)
    capacity: float  # the resource's capacity for that day
    utilization: float  # committed / capacity * 100, a percentage (>100 => over-allocated)


def task_active_on(task: Task, day: date) -> bool:
    """Whether `task` consumes load on `day`.

    Both date endpoints are inclusive. A task contributes load only once it has a
    start date; an open-ended task (start set, end None) is active from its start
    onward. A task with no start_date is unscheduled and never contributes.
    """
    if task.start_date is None or day < task.start_date:
        return False
    return task.end_date is None or day <= task.end_date


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

    Only tasks assigned to `resource` are counted; the function filters the given
    iterable by resource_id, so callers may pass any task list. The range is
    inclusive of both endpoints; an inverted range (start > end) yields [].
    """
    assigned = [t for t in tasks if t.resource_id == resource.id]
    capacity = resource.capacity
    result: list[DayUtilization] = []
    for day in _days(start, end):
        committed = daily_committed_load(assigned, day)
        # capacity > 0 is guaranteed by the API layer; guard the degenerate
        # model-level case so a stray 0-capacity resource can't divide-by-zero.
        if capacity > 0:
            utilization = committed / capacity * 100.0
        else:
            utilization = float("inf") if committed > 0 else 0.0
        result.append(
            DayUtilization(day=day, committed=committed, capacity=capacity, utilization=utilization)
        )
    return result


def resource_schedule(
    session: Session, resource_id: int, start: date, end: date
) -> Optional[list[DayUtilization]]:
    """DB entrypoint: load a resource and its tasks, then compute utilization.

    Returns None if the resource does not exist (so the API layer can 404),
    otherwise the per-day utilization list.
    """
    resource = session.get(Resource, resource_id)
    if resource is None:
        return None
    tasks = session.exec(select(Task).where(Task.resource_id == resource_id)).all()
    return compute_utilization(resource, tasks, start, end)


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

    A day is a conflict only when committed load is *strictly greater* than
    capacity: a fully-booked day (committed == capacity) is not flagged. The
    strict comparison is the off-by-one guard — using >= would falsely flag
    exact full booking. For each flagged day the contributing tasks (those
    active that day, assigned to this resource) are reported.
    """
    assigned = [t for t in tasks if t.resource_id == resource.id]
    conflicts: list[Conflict] = []
    for row in compute_utilization(resource, tasks, start, end):
        if row.committed > row.capacity:
            contributing = [t for t in assigned if task_active_on(t, row.day)]
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
    """DB entrypoint for conflict detection.

    Returns None if the resource does not exist (so the API layer can 404),
    otherwise the list of over-allocated days.
    """
    resource = session.get(Resource, resource_id)
    if resource is None:
        return None
    tasks = session.exec(select(Task).where(Task.resource_id == resource_id)).all()
    return detect_conflicts(resource, tasks, start, end)
