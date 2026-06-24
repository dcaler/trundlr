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
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.models import (
    AppSettings, Project, Resource, ResourceBlockout, ResourceCalBlock,
    ResourceWindow, Task, TaskResource, TaskStatus,
)

_EPS = 1e-6  # tolerance for float hour comparisons

# How far ahead the re-flow scheduler will search for a slot. A task that does
# not fit anywhere in this window is reported as unschedulable rather than being
# stamped with a bogus far-future date (the failure mode of the reverted rework).
SCHED_HORIZON_DAYS = 365


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


def calblock_segments(block: ResourceCalBlock) -> "list[ResourceBlockout]":
    """Convert a CalDAV block (start/end datetimes) into per-day blockout-shaped
    segments the capacity engine and JS shading already understand.

    One transient (un-persisted) ResourceBlockout per calendar day covered:
    a day fully inside the block becomes a full-day blockout (from/to = None);
    a partially-covered day carries clamped "HH:MM" bounds ("24:00" = midnight).
    """
    segments: list[ResourceBlockout] = []
    day = block.start.date()
    last = block.end.date()
    while day <= last:
        day_start = datetime.combine(day, time.min)
        day_end = day_start + timedelta(days=1)
        seg_start = max(block.start, day_start)
        seg_end = min(block.end, day_end)
        if seg_end > seg_start:
            full = seg_start == day_start and seg_end == day_end
            segments.append(ResourceBlockout(
                resource_id=block.resource_id,
                start_date=day,
                end_date=day,
                from_time=None if full else f"{seg_start.hour:02d}:{seg_start.minute:02d}",
                to_time=None if full else (
                    "24:00" if seg_end == day_end else f"{seg_end.hour:02d}:{seg_end.minute:02d}"
                ),
            ))
        day += timedelta(days=1)
    return segments


def _calblocks_for_resource(session: Session, resource_id: int) -> "list[ResourceCalBlock]":
    return list(session.exec(
        select(ResourceCalBlock).where(ResourceCalBlock.resource_id == resource_id)
    ).all())


def _obstacles_for_resource(session: Session, resource_id: int) -> "list[ResourceBlockout]":
    """Manual blockouts plus CalDAV blocks, all in blockout shape."""
    blockouts = _blockouts_for_resource(session, resource_id)
    for block in _calblocks_for_resource(session, resource_id):
        blockouts.extend(calblock_segments(block))
    return blockouts


def resource_schedule(
    session: Session, resource_id: int, start: date, end: date
) -> Optional[list[DayUtilization]]:
    resource = session.get(Resource, resource_id)
    if resource is None:
        return None
    windows = _windows_for_resource(session, resource_id)
    blockouts = _obstacles_for_resource(session, resource_id)
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
    blockouts = _obstacles_for_resource(session, resource_id)
    return detect_conflicts(
        resource,
        _tasks_for_resource(session, resource_id),
        start, end,
        windows=windows or None,
        blockouts=blockouts or None,
    )


# ── Re-flow scheduler ───────────────────────────────────────────────────────
# Priority-driven, dependency-correct, backfilling placement of todo tasks.
#
# Tasks are placed in (project.priority, project.id, task.id) order using a
# readiness loop: a task becomes placeable only once its predecessor is placed,
# so dependencies are honored across resources AND across projects. Each task is
# dropped into the earliest free slot on all of its resources (backfilling gaps
# higher-priority work left behind). A task that does not fit within the search
# horizon, or whose dependency can never resolve, is left untouched and reported
# — never written a bogus date.

Interval = tuple[datetime, datetime]


def _available_intervals_on_day(
    resource: Resource,
    day: date,
    windows: "list[ResourceWindow] | None",
    blockouts: "list[ResourceBlockout] | None",
) -> list[Interval]:
    """The resource's free-to-work datetime intervals on `day` (window minus
    blockouts). Mirrors the hour math in `resource_capacity_on_day`, but returns
    concrete datetime intervals instead of a summed hour count."""
    dow = day.weekday()
    hours: list[tuple[float, float]] = []
    if windows is not None:
        for w in windows:
            if w.day_of_week == dow:
                hours.append((_hhmm_to_hours(w.from_time), _hhmm_to_hours(w.to_time)))
    elif resource.available_days and (resource.available_days & (1 << dow)):
        hours.append(
            (_hhmm_to_hours(resource.available_from), _hhmm_to_hours(resource.available_to))
        )
    if not hours:
        return []
    if blockouts:
        for b in blockouts:
            if not (b.start_date <= day <= b.end_date):
                continue
            if b.from_time is None:
                return []  # full-day blockout
            hours = _subtract_interval(
                hours, _hhmm_to_hours(b.from_time), _hhmm_to_hours(b.to_time)
            )
    day_start = datetime.combine(day, time.min)
    return [
        (day_start + timedelta(hours=s), day_start + timedelta(hours=e))
        for s, e in hours
        if e > s
    ]


def _subtract_busy(intervals: list[Interval], busy: list[Interval]) -> list[Interval]:
    """Remove every busy interval from `intervals` (datetime version of
    `_subtract_interval`)."""
    out = list(intervals)
    for bs, be in busy:
        nxt: list[Interval] = []
        for s, e in out:
            if be <= s or bs >= e:
                nxt.append((s, e))
                continue
            if bs > s:
                nxt.append((s, bs))
            if be < e:
                nxt.append((be, e))
        out = nxt
    return out


def _intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """Pairwise intersection of two interval lists."""
    out: list[Interval] = []
    for s1, e1 in a:
        for s2, e2 in b:
            s, e = max(s1, s2), min(e1, e2)
            if e > s:
                out.append((s, e))
    return out


def _merge_adjacent(ivs: list[Interval]) -> list[Interval]:
    """Merge overlapping or nearly-adjacent intervals (gap ≤ 1 min).
    The 1-minute tolerance bridges the 23:59→00:00 day boundary so that
    resources available 00:00–23:59 every day are treated as continuous."""
    if not ivs:
        return []
    out = [ivs[0]]
    _GAP = timedelta(minutes=1)
    for s, e in ivs[1:]:
        if s <= out[-1][1] + _GAP:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _earliest_slot(
    resources: list[Resource],
    earliest: datetime,
    dur: timedelta,
    busy: dict[int, list[Interval]],
    windows_by_res: dict[int, list[ResourceWindow]],
    blockouts_by_res: dict[int, list[ResourceBlockout]],
) -> Optional[datetime]:
    """Earliest start ≥ `earliest` where a `dur`-long block fits on EVERY listed
    resource at once. Adjacent daily windows (≤ 1 min gap) are merged so that
    24/7 resources (00:00–23:59) are treated as continuous across midnight.
    Returns None if no slot exists within SCHED_HORIZON_DAYS."""
    if not resources:
        return None
    start_day = earliest.date()

    # Build per-resource continuous free-time lists across the full horizon,
    # then intersect. Merging adjacent daily intervals lets tasks span midnight
    # on resources that are available around the clock.
    resource_free: list[list[Interval]] = []
    for r in resources:
        ivs: list[Interval] = []
        for off in range(SCHED_HORIZON_DAYS + 2):
            day = start_day + timedelta(days=off)
            avail = _available_intervals_on_day(
                r, day, windows_by_res.get(r.id), blockouts_by_res.get(r.id)
            )
            ivs.extend(_subtract_busy(avail, busy.get(r.id, [])))
        resource_free.append(_merge_adjacent(sorted(ivs)))

    combined = resource_free[0]
    for other in resource_free[1:]:
        combined = _intersect(combined, other)

    for s, e in combined:
        cand = max(s, earliest)
        if cand + dur <= e:
            return cand

    return None


def _task_duration(task: Task) -> timedelta:
    """Block length to schedule: the duration field (hours), else the existing
    start→end span, else 1 hour."""
    if task.duration:
        return timedelta(hours=task.duration)
    if task.start_date and task.end_date and task.end_date > task.start_date:
        return task.end_date - task.start_date
    return timedelta(hours=1)


def reflow_schedule(session: Session) -> dict:
    """Recompute start/end for all todo tasks, highest-priority project first,
    backfilling resource gaps and honoring dependency chains across resources and
    projects. Commits nothing — the caller commits. Returns a summary dict:
    {changed, pinned, unscheduled: [{id, title, reason}]}."""
    settings = session.get(AppSettings, 1)
    tz = ZoneInfo(settings.timezone if settings else "UTC")
    now = datetime.now(tz).replace(tzinfo=None, second=0, microsecond=0)

    projects = {p.id: p for p in session.exec(select(Project)).all()}
    resources = {r.id: r for r in session.exec(select(Resource)).all()}
    tasks = list(session.exec(select(Task)).all())
    tasks_by_id = {t.id: t for t in tasks}

    res_by_task: dict[int, list[int]] = {}
    for tr in session.exec(select(TaskResource)).all():
        res_by_task.setdefault(tr.task_id, []).append(tr.resource_id)

    windows_by_res: dict[int, list[ResourceWindow]] = {}
    for w in session.exec(select(ResourceWindow)).all():
        windows_by_res.setdefault(w.resource_id, []).append(w)
    blockouts_by_res: dict[int, list[ResourceBlockout]] = {}
    for b in session.exec(select(ResourceBlockout)).all():
        blockouts_by_res.setdefault(b.resource_id, []).append(b)
    for blk in session.exec(select(ResourceCalBlock)).all():
        blockouts_by_res.setdefault(blk.resource_id, []).extend(calblock_segments(blk))

    # Immovable obstacles: in-flight/finished tasks and pinned todos hold their
    # slots so movable work routes around them. end_of seeds dependency
    # resolution for those same tasks.
    busy: dict[int, list[Interval]] = {rid: [] for rid in resources}
    end_of: dict[int, datetime] = {}
    FIXED = {TaskStatus.in_progress, TaskStatus.paused, TaskStatus.done, TaskStatus.failed}
    for t in tasks:
        if not (t.start_date and t.end_date):
            continue
        if t.status in FIXED or (t.status == TaskStatus.todo and t.pinned):
            for rid in res_by_task.get(t.id, []):
                if rid in busy:
                    busy[rid].append((t.start_date, t.end_date))
            end_of[t.id] = t.end_date

    def prio(t: Task) -> tuple[int, int, int]:
        p = projects.get(t.project_id)
        return ((p.priority if p else 3), t.project_id, t.id)

    # Movable: unpinned todos with at least one resource. (Resource-less tasks
    # can't occupy a timeline; like the previous scheduler, they're left alone.)
    remaining = sorted(
        (t for t in tasks
         if t.status == TaskStatus.todo and not t.pinned and res_by_task.get(t.id)),
        key=prio,
    )

    placed: dict[int, Interval] = {}
    unscheduled: list[dict] = []

    # Readiness loop: each pass places the single highest-priority task whose
    # dependency is already resolved, then restarts so newly-unblocked dependents
    # are reconsidered in priority order. Stops when no remaining task is ready.
    guard = 0
    while remaining and guard < 10000:
        guard += 1
        picked = None
        for t in remaining:
            if t.depends_on_id is not None:
                if t.depends_on_id not in end_of:
                    continue  # dependency not (yet) placed — skip
                earliest = max(now, end_of[t.depends_on_id])
            else:
                earliest = now
            rids = [r for r in res_by_task.get(t.id, []) if r in resources]
            slot = _earliest_slot(
                [resources[r] for r in rids],
                earliest, _task_duration(t), busy, windows_by_res, blockouts_by_res,
            )
            if slot is None:
                unscheduled.append({"id": t.id, "title": t.title,
                                    "reason": "no availability within horizon"})
            else:
                end = slot + _task_duration(t)
                placed[t.id] = (slot, end)
                end_of[t.id] = end
                for r in rids:
                    busy[r].append((slot, end))
            picked = t
            break
        if picked is None:
            break  # nothing ready — the rest depend on unresolvable predecessors
        remaining.remove(picked)

    # Anything still remaining never became ready: cyclic, or depends on a
    # blocked / unschedulable / deleted predecessor.
    for t in remaining:
        dep = tasks_by_id.get(t.depends_on_id) if t.depends_on_id else None
        reason = ("dependency was deleted" if (t.dependency_broken or
                  (t.depends_on_id and dep is None))
                  else "blocked by an unscheduled dependency")
        unscheduled.append({"id": t.id, "title": t.title, "reason": reason})

    # Apply: write moved tasks, strip blocked tasks' dates, leave the rest alone.
    changed = 0
    for tid, (start, end) in placed.items():
        t = tasks_by_id[tid]
        if t.start_date is None or abs((start - t.start_date).total_seconds()) >= 60:
            t.start_date, t.end_date = start, end
            session.add(t)
            changed += 1
    for t in tasks:
        if t.status == TaskStatus.blocked and (t.start_date or t.end_date):
            t.start_date = t.end_date = None
            session.add(t)
            changed += 1

    pinned = sum(1 for t in tasks if t.status == TaskStatus.todo and t.pinned)
    return {"changed": changed, "pinned": pinned, "unscheduled": unscheduled}
