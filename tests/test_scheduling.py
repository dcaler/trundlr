"""Unit tests for the hours-based scheduling engine.

Capacity = available hours per day (window length minus blockouts).
Committed = assigned task-hours (interval overlap with the day).
net = capacity - committed (positive = spare, negative = over-allocated).
"""

from datetime import date, datetime, timedelta

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
    task_hours_on_day,
)

D = date


def _dt(y, mo, d, h=0, m=0):
    return datetime(y, mo, d, h, m)


def _mk_resource(kind=ResourceKind.human, rid=1, available_from="09:00",
                 available_to="17:00", available_days=31):
    r = Resource(name="r", kind=kind, available_from=available_from,
                 available_to=available_to, available_days=available_days)
    r.id = rid
    return r


# --- task_hours_on_day --------------------------------------------------------


def test_task_hours_single_day():
    t = Task(title="t", project_id=1, start_date=_dt(2026, 6, 1, 9), end_date=_dt(2026, 6, 1, 17))
    assert task_hours_on_day(t, D(2026, 6, 1)) == pytest.approx(8.0)
    assert task_hours_on_day(t, D(2026, 6, 2)) == pytest.approx(0.0)


def test_task_hours_spanning_midnight():
    t = Task(title="t", project_id=1, start_date=_dt(2026, 6, 1, 22), end_date=_dt(2026, 6, 2, 2))
    assert task_hours_on_day(t, D(2026, 6, 1)) == pytest.approx(2.0)
    assert task_hours_on_day(t, D(2026, 6, 2)) == pytest.approx(2.0)


def test_task_hours_from_duration_when_open_end():
    t = Task(title="t", project_id=1, start_date=_dt(2026, 6, 1, 9), end_date=None, duration=4)
    assert task_hours_on_day(t, D(2026, 6, 1)) == pytest.approx(4.0)


def test_task_hours_open_ended_no_duration_is_zero():
    t = Task(title="t", project_id=1, start_date=_dt(2026, 6, 1, 9), end_date=None)
    assert task_hours_on_day(t, D(2026, 6, 1)) == 0.0


def test_unscheduled_task_zero_hours():
    t = Task(title="t", project_id=1, start_date=None, end_date=None)
    assert task_hours_on_day(t, D(2026, 6, 1)) == 0.0


def test_daily_committed_load_sums_hours():
    tasks = [
        Task(title="a", project_id=1, start_date=_dt(2026, 6, 1, 9), end_date=_dt(2026, 6, 1, 12)),
        Task(title="b", project_id=1, start_date=_dt(2026, 6, 1, 13), end_date=_dt(2026, 6, 1, 17)),
    ]
    assert daily_committed_load(tasks, D(2026, 6, 1)) == pytest.approx(7.0)
    assert daily_committed_load(tasks, D(2026, 6, 2)) == pytest.approx(0.0)


# --- task_active_on predicate -------------------------------------------------


def test_task_active_on_boundaries():
    t = Task(title="t", project_id=1, start_date=_dt(2026, 6, 1, 9), end_date=_dt(2026, 6, 3, 17))
    assert not task_active_on(t, D(2026, 5, 31))
    assert task_active_on(t, D(2026, 6, 1))
    assert task_active_on(t, D(2026, 6, 2))
    assert task_active_on(t, D(2026, 6, 3))
    assert not task_active_on(t, D(2026, 6, 4))


def test_task_active_on_unscheduled():
    assert not task_active_on(Task(title="t", project_id=1, start_date=None, end_date=None), D(2026, 6, 1))


# --- compute_utilization: table-driven (committed, capacity, net) hours --------

CASES = [
    (
        "single_8h_task_at_capacity",
        ResourceKind.human, 31,
        [(_dt(2026, 6, 1, 9), _dt(2026, 6, 1, 17))],
        D(2026, 6, 1), D(2026, 6, 2),
        [(8.0, 8.0, 0.0), (0.0, 8.0, 8.0)],
    ),
    (
        "two_overlapping_over",
        ResourceKind.human, 31,
        [(_dt(2026, 6, 1, 9), _dt(2026, 6, 1, 17)), (_dt(2026, 6, 1, 9), _dt(2026, 6, 1, 17))],
        D(2026, 6, 1), D(2026, 6, 1),
        [(16.0, 8.0, -8.0)],
    ),
    (
        "half_day_under",
        ResourceKind.human, 31,
        [(_dt(2026, 6, 1, 9), _dt(2026, 6, 1, 13))],
        D(2026, 6, 1), D(2026, 6, 1),
        [(4.0, 8.0, 4.0)],
    ),
    (
        "weekend_unavailable_with_task_over",
        ResourceKind.human, 31,
        [(_dt(2026, 6, 6, 9), _dt(2026, 6, 6, 12))],  # Saturday, capacity 0
        D(2026, 6, 6), D(2026, 6, 6),
        [(3.0, 0.0, -3.0)],
    ),
    (
        "empty_day_full_spare",
        ResourceKind.human, 31,
        [],
        D(2026, 6, 1), D(2026, 6, 1),
        [(0.0, 8.0, 8.0)],
    ),
]


@pytest.mark.parametrize(
    "name,kind,available_days,specs,start,end,expected",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_compute_utilization_table(name, kind, available_days, specs, start, end, expected):
    resource = _mk_resource(kind=kind, available_days=available_days)
    tasks = [Task(title="t", project_id=1, start_date=s, end_date=e) for s, e in specs]
    result = compute_utilization(resource, tasks, start, end)

    assert len(result) == len(expected)
    expected_day = start
    for row, (exp_committed, exp_capacity, exp_net) in zip(result, expected):
        assert row.day == expected_day
        assert row.committed == pytest.approx(exp_committed)
        assert row.capacity == pytest.approx(exp_capacity)
        assert row.net == pytest.approx(exp_net)
        expected_day += timedelta(days=1)


def test_compute_utilization_inverted_range_is_empty():
    resource = _mk_resource()
    assert compute_utilization(resource, [], D(2026, 6, 5), D(2026, 6, 1)) == []


# --- resource_capacity_on_day (hours) -----------------------------------------


def test_capacity_hours_weekday():
    r = _mk_resource(available_from="09:00", available_to="17:00", available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(8.0)  # Monday


def test_capacity_hours_weekend_is_zero():
    r = _mk_resource(available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 6)) == pytest.approx(0.0)  # Saturday


def test_capacity_custom_hours():
    r = _mk_resource(available_from="08:30", available_to="16:30", available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(8.0)


def test_capacity_partial_week_hours():
    r = _mk_resource(available_from="09:00", available_to="13:00", available_days=3)  # Mon+Tue, 4h
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(4.0)  # Monday
    assert resource_capacity_on_day(r, D(2026, 6, 3)) == pytest.approx(0.0)  # Wednesday


def test_capacity_gpu_all_week():
    r = _mk_resource(kind=ResourceKind.gpu, available_days=127)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(8.0)  # Mon
    assert resource_capacity_on_day(r, D(2026, 6, 6)) == pytest.approx(8.0)  # Sat


# --- windows ------------------------------------------------------------------


def test_windows_override_hours():
    r = _mk_resource(available_days=31)
    windows = [ResourceWindow(resource_id=1, day_of_week=6, from_time="10:00", to_time="18:00")]  # Sun 8h
    assert resource_capacity_on_day(r, D(2026, 6, 7), windows=windows) == pytest.approx(8.0)  # Sunday
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=windows) == pytest.approx(0.0)  # Monday


def test_multiple_windows_same_day_sum_hours():
    r = _mk_resource()
    windows = [
        ResourceWindow(resource_id=1, day_of_week=0, from_time="09:00", to_time="12:00"),  # 3h
        ResourceWindow(resource_id=1, day_of_week=0, from_time="13:00", to_time="17:00"),  # 4h
    ]
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=windows) == pytest.approx(7.0)


def test_empty_windows_means_zero_hours():
    r = _mk_resource(available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=[]) == pytest.approx(0.0)


def test_windows_none_falls_back_to_simple_schedule():
    r = _mk_resource(available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=None) == pytest.approx(8.0)


# --- blockouts ----------------------------------------------------------------


def test_full_day_blockout_zero_hours():
    r = _mk_resource(available_days=127)
    blockouts = [ResourceBlockout(resource_id=1, start_date=D(2026, 6, 1), end_date=D(2026, 6, 3))]
    assert resource_capacity_on_day(r, D(2026, 6, 1), blockouts=blockouts) == pytest.approx(0.0)
    assert resource_capacity_on_day(r, D(2026, 6, 3), blockouts=blockouts) == pytest.approx(0.0)
    assert resource_capacity_on_day(r, D(2026, 6, 4), blockouts=blockouts) == pytest.approx(8.0)


def test_partial_blockout_subtracts_hours():
    r = _mk_resource(available_days=127)  # 09:00-17:00 = 8h
    blockouts = [ResourceBlockout(
        resource_id=1, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1),
        from_time="12:00", to_time="13:00",
    )]
    assert resource_capacity_on_day(r, D(2026, 6, 1), blockouts=blockouts) == pytest.approx(7.0)


def test_blockout_outside_range_does_not_subtract():
    r = _mk_resource(available_days=127)
    blockouts = [ResourceBlockout(resource_id=1, start_date=D(2026, 6, 5), end_date=D(2026, 6, 10))]
    assert resource_capacity_on_day(r, D(2026, 6, 1), blockouts=blockouts) == pytest.approx(8.0)


def test_full_day_blockout_with_windows():
    r = _mk_resource(available_days=31)
    windows = [ResourceWindow(resource_id=1, day_of_week=0, from_time="09:00", to_time="17:00")]
    blockouts = [ResourceBlockout(resource_id=1, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1))]
    assert resource_capacity_on_day(r, D(2026, 6, 1), windows=windows, blockouts=blockouts) == pytest.approx(0.0)


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
    resource = Resource(name="Alice", kind=ResourceKind.human,
                        available_from="09:00", available_to="17:00", available_days=31)
    session.add_all([project, resource])
    session.commit()
    # 8h task on Jun 1 (Mon)
    task = Task(title="t", project_id=project.id,
                start_date=_dt(2026, 6, 1, 9), end_date=_dt(2026, 6, 1, 17))
    session.add(task)
    session.flush()
    session.add(TaskResource(task_id=task.id, resource_id=resource.id))
    session.commit()

    sched = resource_schedule(session, resource.id, D(2026, 6, 1), D(2026, 6, 3))
    assert [round(r.net, 2) for r in sched] == pytest.approx([0.0, 8.0, 8.0])


def test_resource_schedule_missing_resource_returns_none(session):
    assert resource_schedule(session, 9999, D(2026, 6, 1), D(2026, 6, 2)) is None


def test_resource_schedule_respects_windows(session):
    project = Project(name="P")
    resource = Resource(name="R", kind=ResourceKind.human,
                        available_from="09:00", available_to="17:00", available_days=31)
    session.add_all([project, resource])
    session.commit()
    # Window for Saturday (5) only, 10:00-14:00 = 4h
    session.add(ResourceWindow(resource_id=resource.id, day_of_week=5, from_time="10:00", to_time="14:00"))
    session.commit()

    sched = {r.day: r for r in resource_schedule(session, resource.id, D(2026, 6, 6), D(2026, 6, 7))}
    assert sched[D(2026, 6, 6)].capacity == pytest.approx(4.0)  # Saturday window
    assert sched[D(2026, 6, 7)].capacity == pytest.approx(0.0)  # Sunday, no window


def test_resource_schedule_respects_blockouts(session):
    project = Project(name="P")
    resource = Resource(name="R", kind=ResourceKind.human,
                        available_from="09:00", available_to="17:00", available_days=127)
    session.add_all([project, resource])
    session.commit()
    session.add(ResourceBlockout(resource_id=resource.id, start_date=D(2026, 6, 2), end_date=D(2026, 6, 2)))
    session.commit()

    sched = {r.day: r for r in resource_schedule(session, resource.id, D(2026, 6, 1), D(2026, 6, 3))}
    assert sched[D(2026, 6, 1)].capacity == pytest.approx(8.0)
    assert sched[D(2026, 6, 2)].capacity == pytest.approx(0.0)  # blocked
    assert sched[D(2026, 6, 3)].capacity == pytest.approx(8.0)
