"""Scheduling engine: hours-based utilization.

Each resource has availability measured in HOURS per day — derived from its
availability window (available_from→available_to on available_days) or from
per-day ResourceWindows, minus any blockouts. A task contributes the number of
hours its [start, end] interval overlaps a given calendar day. A day is
over-allocated (a conflict) when assigned task-hours exceed available hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Iterator, Optional

from sqlmodel import Session, select

from app.models import Resource, ResourceBlockout, ResourceWindow, Task, TaskResource

_EPS = 1e-6  # tolerance for float hour comparisons


@dataclass(frozen=True)
class DayUtilization:
    day: date
    committed: float  # hours of tasks assigned that day
    capacity: float   # hours of availability that day
    net: float        # capacity - committed (positive = spare, negative = over)


def _hhmm_to_hours(value: str) -> float:
    h, m = value.split(":")
    return int(h) + int(m) / 60.0


def _subtract_interval(
    intervals: list[tuple[float, float]], lo: float, hi: float
) -> list[tuple[float, float]]:
    """Remove [lo, hi) from each (start, end) interval, returning the remainder."""
    out: list[tuple[float, float]] = []
    for s, e in intervals:
        if hi <= s or lo >= e:
            out.append((s, e))
            continue
        if lo > s:
            out.append((s, lo))
        if hi < e:
            out.append((hi, e))
    return out


def resource_capacity_on_day(
    resource: Resource,
    day: date,
    windows: "list[ResourceWindow] | None" = None,
    blockouts: "list[ResourceBlockout] | None" = None,
) -> float:
    """Available HOURS on `day`.

    When `windows` is not None it defines availability (per day_of_week),
    overriding the simple available_from/to/days fields; an empty list means 0.
    Full-day blockouts zero the day; partial blockouts subtract their overlap.
    """
    dow = day.weekday()
    intervals: list[tuple[float, float]] = []
    if windows is not None:
        for w in windows:
            if w.day_of_week == dow:
                intervals.append((_hhmm_to_hours(w.from_time), _hhmm_to_hours(w.to_time)))
    elif resource.available_days and (resource.available_days & (1 << dow)):
        intervals.append(
            (_hhmm_to_hours(resource.available_from), _hhmm_to_hours(resource.available_to))
        )

    if not intervals:
        return 0.0

    if blockouts:
        for b in blockouts:
            if not (b.start_date <= day <= b.end_date):
                continue
            if b.from_time is None:
                return 0.0  # full-day blockout
            intervals = _subtract_interval(
                intervals, _hhmm_to_hours(b.from_time), _hhmm_to_hours(b.to_time)
            )

    return round(sum(max(0.0, e - s) for s, e in intervals), 6)


def _as_datetime(value: date | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.combine(value, time.min)


def _task_end(task: Task) -> Optional[datetime]:
    """Effective end datetime: explicit end_date, else start+duration, else None."""
    if task.end_date is not None:
        return _as_datetime(task.end_date)
    if task.duration:
        return _as_datetime(task.start_date) + timedelta(hours=task.duration)
    return None


def task_hours_on_day(task: Task, day: date) -> float:
    """Hours the task's [start, end] interval overlaps `day`.

    Unscheduled tasks (no start) and open-ended tasks with neither an end_date
    nor a duration contribute 0 (unknown length).
    """
    if task.start_date is None:
        return 0.0
    start = _as_datetime(task.start_date)
    end = _task_end(task)
    if end is None or end <= start:
        return 0.0
    day_start = datetime.combine(day, time.min)
    day_end = day_start + timedelta(days=1)
    lo = max(start, day_start)
    hi = min(end, day_end)
    return max(0.0, (hi - lo).total_seconds() / 3600.0)


def task_active_on(task: Task, day: date) -> bool:
    """Whether the task occupies any time on `day` (overlap hours > 0)."""
    return task_hours_on_day(task, day) > 0.0


def daily_committed_load(tasks: Iterable[Task], day: date) -> float:
    """Total assigned hours on `day` across `tasks`."""
    return round(sum(task_hours_on_day(t, day) for t in tasks), 6)


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
    """Per-day committed/available hours for one resource over [start, end].

    `tasks` must already be pre-filtered to those assigned to `resource`.
    Inverted range yields [].
    """
    task_list = list(tasks)
    result: list[DayUtilization] = []
    for day in _days(start, end):
        committed = daily_committed_load(task_list, day)
        capacity = resource_capacity_on_day(resource, day, windows, blockouts)
        result.append(
            DayUtilization(
                day=day,
                committed=committed,
                capacity=capacity,
                net=round(capacity - committed, 6),
            )
        )
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
    """Days in [start, end] where assigned hours exceed available hours.

    `tasks` must already be pre-filtered to those assigned to `resource`.
    At-or-under capacity is not a conflict; only committed > capacity.
    """
    task_list = list(tasks)
    conflicts: list[Conflict] = []
    for row in compute_utilization(resource, task_list, start, end, windows, blockouts):
        if row.committed > row.capacity + _EPS:
            contributing = [t for t in task_list if task_hours_on_day(t, row.day) > 0.0]
            conflicts.append(Conflict(
                day=row.day,
                committed=row.committed,
                capacity=row.capacity,
                overage=round(row.committed - row.capacity, 6),
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
