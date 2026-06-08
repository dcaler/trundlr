from datetime import date, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Project, Resource, ResourceBlockout, ResourceKind, ResourceWindow, Task, TaskResource
from app.scheduling import (
    compute_utilization,
    daily_committed_load,
    resource_capacity_on_day,
    resource_schedule,
    task_active_on,
)

D = date


def _mk_resource(kind=ResourceKind.human, rid=1, available_from="09:00",
                 available_to="17:00", available_days=31):
    r = Resource(name="r", kind=kind, available_from=available_from,
                 available_to=available_to, available_days=available_days)
    r.id = rid
    return r


def _mk_tasks(specs):
    return [
        Task(title="t", project_id=1, start_date=start, end_date=end)
        for start, end in specs
    ]


# --- task_active_on predicate -------------------------------------------------


def test_task_active_on_boundaries():
    t = Task(title="t", project_id=1, start_date=D(2026, 6, 1), end_date=D(2026, 6, 3))
    assert not task_active_on(t, D(2026, 5, 31))
    assert task_active_on(t, D(2026, 6, 1))
    assert task_active_on(t, D(2026, 6, 2))
    assert task_active_on(t, D(2026, 6, 3))
    assert not task_active_on(t, D(2026, 6, 4))


def test_task_active_on_open_ended():
    t = Task(title="t", project_id=1, start_date=D(2026, 6, 1), end_date=None)
    assert not task_active_on(t, D(2026, 5, 31))
    assert task_active_on(t, D(2026, 6, 1))
    assert task_active_on(t, D(2027, 1, 1))


def test_task_active_on_unscheduled():
    no_start = Task(title="t", project_id=1, start_date=None, end_date=None)
    assert not task_active_on(no_start, D(2026, 6, 1))
    end_only = Task(title="t", project_id=1, start_date=None, end_date=D(2026, 6, 30))
    assert not task_active_on(end_only, D(2026, 6, 1))


# --- daily_committed_load -----------------------------------------------------


def test_daily_committed_load_counts_active_tasks():
    tasks = _mk_tasks(
        [
            (D(2026, 6, 1), D(2026, 6, 2)),
            (D(2026, 6, 2), D(2026, 6, 3)),
        ]
    )
    assert daily_committed_load(tasks, D(2026, 6, 1)) == 1.0
    assert daily_committed_load(tasks, D(2026, 6, 2)) == 2.0
    assert daily_committed_load(tasks, D(2026, 6, 3)) == 1.0
    assert daily_committed_load(tasks, D(2026, 6, 4)) == 0.0


# --- compute_utilization: table-driven ----------------------------------------

# name, kind, available_days, task specs [(start, end)], range start, range end,
# expected per-day [(committed, utilization%)]
CASES = [
    (
        # 1 task Mon-Wed; Mon-Fri availability → 100% each available day.
        "single_task",
        ResourceKind.human,
        31,
        [(D(2026, 6, 1), D(2026, 6, 3))],
        D(2026, 6, 1),
        D(2026, 6, 3),
        [(1.0, 100.0), (1.0, 100.0), (1.0, 100.0)],
    ),
    (
        # Two tasks overlap on 6/2-6/3 → committed=2 → 200% conflict.
        "overlapping_tasks_conflict",
        ResourceKind.human,
        31,
        [(D(2026, 6, 1), D(2026, 6, 3)), (D(2026, 6, 2), D(2026, 6, 4))],
        D(2026, 6, 1),
        D(2026, 6, 4),
        [(1.0, 100.0), (2.0, 200.0), (2.0, 200.0), (1.0, 100.0)],
    ),
    (
        # Task starts before the window and ends mid-window; zero-task day follows.
        "partial_overlap_and_zero_day",
        ResourceKind.human,
        31,
        [(D(2026, 5, 28), D(2026, 6, 2))],
        D(2026, 6, 1),
        D(2026, 6, 3),
        [(1.0, 100.0), (1.0, 100.0), (0.0, 0.0)],
    ),
    (
        "single_day_task_then_zero_days",
        ResourceKind.human,
        31,
        [(D(2026, 6, 1), D(2026, 6, 1))],
        D(2026, 6, 1),
        D(2026, 6, 3),
        [(1.0, 100.0), (0.0, 0.0), (0.0, 0.0)],
    ),
    (
        # Jun 29 (Mon) – Jul 2 (Thu), checked through Jul 3 (Fri) — crosses month boundary.
        "month_boundary",
        ResourceKind.human,
        31,
        [(D(2026, 6, 29), D(2026, 7, 2))],
        D(2026, 6, 29),
        D(2026, 7, 3),
        [(1.0, 100.0), (1.0, 100.0), (1.0, 100.0), (1.0, 100.0), (0.0, 0.0)],
    ),
    (
        "open_ended_task",
        ResourceKind.human,
        31,
        [(D(2026, 6, 1), None)],
        D(2026, 6, 1),
        D(2026, 6, 5),
        [(1.0, 100.0)] * 5,
    ),
    (
        # GPU with all-week availability; 2 concurrent tasks → 200% conflict.
        "gpu_always_available_two_tasks_conflict",
        ResourceKind.gpu,
        127,
        [(D(2026, 6, 1), D(2026, 6, 2)), (D(2026, 6, 1), D(2026, 6, 2))],
        D(2026, 6, 1),
        D(2026, 6, 2),
        [(2.0, 200.0), (2.0, 200.0)],
    ),
    (
        "unscheduled_task_ignored",
        ResourceKind.human,
        31,
        [(None, None)],
        D(2026, 6, 1),
        D(2026, 6, 2),
        [(0.0, 0.0), (0.0, 0.0)],
    ),
]


@pytest.mark.parametrize(
    "name,kind,available_days,specs,start,end,expected",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_compute_utilization_table(name, kind, available_days, specs, start, end, expected):
    resource = _mk_resource(kind=kind, available_days=available_days)
    tasks = _mk_tasks(specs)
    result = compute_utilization(resource, tasks, start, end)

    assert len(result) == len(expected)
    expected_day = start
    for row, (exp_committed, exp_util) in zip(result, expected):
        assert row.day == expected_day
        assert row.committed == pytest.approx(exp_committed)
        assert row.utilization == pytest.approx(exp_util)
        expected_day += timedelta(days=1)


def test_compute_utilization_single_task():
    # Jun 1 2026 = Monday; Mon-Fri availability → capacity 1.0
    resource = _mk_resource(rid=1)
    mine = Task(title="mine", project_id=1, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1))
    result = compute_utilization(resource, [mine], D(2026, 6, 1), D(2026, 6, 1))
    assert result[0].committed == 1.0
    assert result[0].capacity == pytest.approx(1.0)
    assert result[0].utilization == pytest.approx(100.0)


def test_compute_utilization_inverted_range_is_empty():
    resource = _mk_resource()
    assert compute_utilization(resource, [], D(2026, 6, 5), D(2026, 6, 1)) == []


# --- resource_capacity_on_day -------------------------------------------------


def test_capacity_on_weekday():
    r = _mk_resource(available_from="09:00", available_to="17:00", available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(1.0)  # Monday


def test_capacity_on_weekend_is_zero():
    r = _mk_resource(available_from="09:00", available_to="17:00", available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 6)) == pytest.approx(0.0)  # Saturday


def test_capacity_with_custom_hours():
    r = _mk_resource(available_from="08:30", available_to="16:30", available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(1.0)


def test_capacity_partial_week():
    # Only Mon+Tue (bits 0+1 = 3)
    r = _mk_resource(available_from="09:00", available_to="13:00", available_days=3)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(1.0)  # Monday
    assert resource_capacity_on_day(r, D(2026, 6, 3)) == pytest.approx(0.0)  # Wednesday


def test_capacity_compute_resource():
    # cpu/gpu use available_days like humans; 127 = all 7 days → 1.0 always
    r = _mk_resource(kind=ResourceKind.gpu, available_days=127)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(1.0)  # Mon
    assert resource_capacity_on_day(r, D(2026, 6, 6)) == pytest.approx(1.0)  # Sat


# --- resource_schedule: DB entrypoint -----------------------------------------


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_resource_schedule_db(session):
    project = Project(name="P")
    # Jun 1-3 2026 = Mon-Wed; Mon-Fri availability → capacity 1.0 each day
    resource = Resource(name="Alice", kind=ResourceKind.human,
                        available_from="09:00", available_to="17:00", available_days=31)
    session.add_all([project, resource])
    session.commit()
    task = Task(
        title="t",
        project_id=project.id,
        start_date=D(2026, 6, 1),
        end_date=D(2026, 6, 2),
    )
    session.add(task)
    session.flush()
    session.add(TaskResource(task_id=task.id, resource_id=resource.id))
    session.commit()

    sched = resource_schedule(session, resource.id, D(2026, 6, 1), D(2026, 6, 3))
    assert [r.utilization for r in sched] == pytest.approx([100.0, 100.0, 0.0])


def test_resource_schedule_missing_resource_returns_none(session):
    assert resource_schedule(session, 9999, D(2026, 6, 1), D(2026, 6, 2)) is None


# --- resource_capacity_on_day with windows ------------------------------------


def test_windows_override_available_days():
    r = _mk_resource(available_days=31)  # Mon-Fri by default
    # Window only on Sunday (6) → available on Sun, not on Mon
    windows = [ResourceWindow(resource_id=1, day_of_week=6, from_time="10:00", to_time="18:00")]
    assert resource_capacity_on_day(r, D(2026, 6, 7), windows=windows) == pytest.approx(1.0)  # Sunday
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=windows) == pytest.approx(0.0)  # Monday


def test_multiple_windows_same_day_still_capacity_one():
    r = _mk_resource()
    windows = [
        ResourceWindow(resource_id=1, day_of_week=0, from_time="09:00", to_time="12:00"),
        ResourceWindow(resource_id=1, day_of_week=0, from_time="13:00", to_time="17:00"),
    ]
    # Both windows on Monday → 1.0 capacity (day-level, not window-level)
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=windows) == pytest.approx(1.0)


def test_empty_windows_means_no_available_days():
    r = _mk_resource(available_days=31)
    # windows=[] (not None) means custom schedule with no windows → 0.0 every day
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=[]) == pytest.approx(0.0)


def test_windows_none_falls_back_to_simple_schedule():
    r = _mk_resource(available_days=31)
    # windows=None → use available_days bitmask
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=None) == pytest.approx(1.0)


# --- resource_capacity_on_day with blockouts ----------------------------------


def test_full_day_blockout_blocks_capacity():
    r = _mk_resource(available_days=127)
    blockouts = [ResourceBlockout(resource_id=1, start_date=D(2026, 6, 1), end_date=D(2026, 6, 3))]
    assert resource_capacity_on_day(r, D(2026, 6, 1), blockouts=blockouts) == pytest.approx(0.0)
    assert resource_capacity_on_day(r, D(2026, 6, 3), blockouts=blockouts) == pytest.approx(0.0)
    assert resource_capacity_on_day(r, D(2026, 6, 4), blockouts=blockouts) == pytest.approx(1.0)


def test_partial_day_blockout_does_not_block_capacity():
    r = _mk_resource(available_days=127)
    blockouts = [ResourceBlockout(
        resource_id=1, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1),
        from_time="12:00", to_time="13:00",
    )]
    # Partial-day blockout: day-level scheduling still sees this day as available
    assert resource_capacity_on_day(r, D(2026, 6, 1), blockouts=blockouts) == pytest.approx(1.0)


def test_blockout_outside_range_does_not_block():
    r = _mk_resource(available_days=127)
    blockouts = [ResourceBlockout(resource_id=1, start_date=D(2026, 6, 5), end_date=D(2026, 6, 10))]
    assert resource_capacity_on_day(r, D(2026, 6, 1), blockouts=blockouts) == pytest.approx(1.0)


def test_blockout_with_windows():
    r = _mk_resource(available_days=31)
    windows = [ResourceWindow(resource_id=1, day_of_week=0, from_time="09:00", to_time="17:00")]
    blockouts = [ResourceBlockout(resource_id=1, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1))]
    # Monday has a window but also a full-day blockout → 0.0
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=windows, blockouts=blockouts) == pytest.approx(0.0)
    # Tuesday has a window but no blockout → 1.0
    windows2 = windows + [ResourceWindow(resource_id=1, day_of_week=1, from_time="09:00", to_time="17:00")]
    assert resource_capacity_on_day(r, D(2026, 6, 2), windows=windows2, blockouts=blockouts) == pytest.approx(1.0)


# --- resource_schedule uses windows/blockouts via DB --------------------------


def test_resource_schedule_respects_windows(session):
    project = Project(name="P")
    resource = Resource(name="R", kind=ResourceKind.human,
                        available_from="09:00", available_to="17:00", available_days=31)
    session.add_all([project, resource])
    session.commit()

    # Add a window for Saturday (day_of_week=5) only
    window = ResourceWindow(resource_id=resource.id, day_of_week=5, from_time="10:00", to_time="14:00")
    session.add(window)

    task = Task(title="t", project_id=project.id, start_date=D(2026, 6, 6), end_date=D(2026, 6, 6))
    session.add(task)
    session.flush()
    session.add(TaskResource(task_id=task.id, resource_id=resource.id))
    session.commit()

    # Jun 6 2026 = Saturday; window defined → capacity 1.0 → 100%
    # Jun 7 2026 = Sunday; no window → capacity 0.0 → 999%? No, 0 committed → 0%
    sched = {r.day: r for r in resource_schedule(session, resource.id, D(2026, 6, 6), D(2026, 6, 7))}
    assert sched[D(2026, 6, 6)].capacity == pytest.approx(1.0)
    assert sched[D(2026, 6, 6)].utilization == pytest.approx(100.0)
    assert sched[D(2026, 6, 7)].capacity == pytest.approx(0.0)


def test_resource_schedule_respects_blockouts(session):
    project = Project(name="P")
    resource = Resource(name="R", kind=ResourceKind.human,
                        available_from="09:00", available_to="17:00", available_days=127)
    session.add_all([project, resource])
    session.commit()

    # Blockout Jun 2
    blockout = ResourceBlockout(resource_id=resource.id, start_date=D(2026, 6, 2), end_date=D(2026, 6, 2))
    session.add(blockout)

    task = Task(title="t", project_id=project.id, start_date=D(2026, 6, 1), end_date=D(2026, 6, 3))
    session.add(task)
    session.flush()
    session.add(TaskResource(task_id=task.id, resource_id=resource.id))
    session.commit()

    sched = {r.day: r for r in resource_schedule(session, resource.id, D(2026, 6, 1), D(2026, 6, 3))}
    # Jun 1: available, 1 task → 100%
    assert sched[D(2026, 6, 1)].utilization == pytest.approx(100.0)
    # Jun 2: blocked → capacity 0, task active → conflict (999%)
    assert sched[D(2026, 6, 2)].capacity == pytest.approx(0.0)
    assert sched[D(2026, 6, 2)].committed == pytest.approx(1.0)
    # Jun 3: available again → 100%
    assert sched[D(2026, 6, 3)].utilization == pytest.approx(100.0)
