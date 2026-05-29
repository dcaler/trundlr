from datetime import date, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Project, Resource, ResourceKind, Task
from app.scheduling import (
    compute_utilization,
    daily_committed_load,
    resource_capacity_on_day,
    resource_schedule,
    task_active_on,
)

D = date


def _mk_resource(capacity=None, kind=ResourceKind.human, rid=1,
                 available_from="09:00", available_to="17:00", available_days=31):
    if kind == ResourceKind.human:
        r = Resource(name="r", kind=kind, available_from=available_from,
                     available_to=available_to, available_days=available_days)
    else:
        r = Resource(name="r", kind=kind, capacity=capacity)
    r.id = rid
    return r


def _mk_tasks(specs, rid=1):
    return [
        Task(title="t", project_id=1, resource_id=rid, load=load, start_date=start, end_date=end)
        for load, start, end in specs
    ]


# --- task_active_on predicate -------------------------------------------------


def test_task_active_on_boundaries():
    t = Task(title="t", project_id=1, load=1.0, start_date=D(2026, 6, 1), end_date=D(2026, 6, 3))
    assert not task_active_on(t, D(2026, 5, 31))  # day before start
    assert task_active_on(t, D(2026, 6, 1))  # start inclusive
    assert task_active_on(t, D(2026, 6, 2))
    assert task_active_on(t, D(2026, 6, 3))  # end inclusive
    assert not task_active_on(t, D(2026, 6, 4))  # day after end


def test_task_active_on_open_ended():
    t = Task(title="t", project_id=1, load=1.0, start_date=D(2026, 6, 1), end_date=None)
    assert not task_active_on(t, D(2026, 5, 31))
    assert task_active_on(t, D(2026, 6, 1))
    assert task_active_on(t, D(2027, 1, 1))  # no end => active arbitrarily far out


def test_task_active_on_unscheduled():
    no_start = Task(title="t", project_id=1, load=1.0, start_date=None, end_date=None)
    assert not task_active_on(no_start, D(2026, 6, 1))
    # An end with no start is still unscheduled: without a known start the engine
    # can't say which days are loaded, so it contributes nothing.
    end_only = Task(title="t", project_id=1, load=1.0, start_date=None, end_date=D(2026, 6, 30))
    assert not task_active_on(end_only, D(2026, 6, 1))


# --- daily_committed_load -----------------------------------------------------


def test_daily_committed_load_sums_active_only():
    tasks = _mk_tasks(
        [
            (3.0, D(2026, 6, 1), D(2026, 6, 2)),
            (5.0, D(2026, 6, 2), D(2026, 6, 3)),
        ]
    )
    assert daily_committed_load(tasks, D(2026, 6, 1)) == 3.0
    assert daily_committed_load(tasks, D(2026, 6, 2)) == 8.0
    assert daily_committed_load(tasks, D(2026, 6, 3)) == 5.0
    assert daily_committed_load(tasks, D(2026, 6, 4)) == 0.0


# --- compute_utilization: table-driven ----------------------------------------

# name, capacity, kind, task specs [(load, start, end)], range start, range end,
# expected per-day [(committed, utilization%)] aligned to consecutive days.
CASES = [
    (
        "single_task",
        8.0,
        ResourceKind.human,
        [(4.0, D(2026, 6, 1), D(2026, 6, 3))],
        D(2026, 6, 1),
        D(2026, 6, 3),
        [(4.0, 50.0), (4.0, 50.0), (4.0, 50.0)],
    ),
    (
        "overlapping_tasks",
        8.0,
        ResourceKind.human,
        [(4.0, D(2026, 6, 1), D(2026, 6, 3)), (2.0, D(2026, 6, 2), D(2026, 6, 4))],
        D(2026, 6, 1),
        D(2026, 6, 4),
        [(4.0, 50.0), (6.0, 75.0), (6.0, 75.0), (2.0, 25.0)],
    ),
    (
        # Task starts before the window and ends mid-window: partial overlap,
        # followed by a zero-task day.
        "partial_overlap_and_zero_day",
        8.0,
        ResourceKind.human,
        [(8.0, D(2026, 5, 28), D(2026, 6, 2))],
        D(2026, 6, 1),
        D(2026, 6, 3),
        [(8.0, 100.0), (8.0, 100.0), (0.0, 0.0)],
    ),
    (
        "single_day_task_then_zero_days",
        8.0,
        ResourceKind.human,
        [(4.0, D(2026, 6, 1), D(2026, 6, 1))],
        D(2026, 6, 1),
        D(2026, 6, 3),
        [(4.0, 50.0), (0.0, 0.0), (0.0, 0.0)],
    ),
    (
        # Jun 29 (Mon) -> Jul 2 (Thu), checked through Jul 3 (Fri) — crosses month boundary.
        "month_boundary",
        8.0,
        ResourceKind.human,
        [(2.0, D(2026, 6, 29), D(2026, 7, 2))],
        D(2026, 6, 29),
        D(2026, 7, 3),
        [(2.0, 25.0), (2.0, 25.0), (2.0, 25.0), (2.0, 25.0), (0.0, 0.0)],
    ),
    (
        "open_ended_task",
        8.0,
        ResourceKind.human,
        [(4.0, D(2026, 6, 1), None)],
        D(2026, 6, 1),
        D(2026, 6, 5),
        [(4.0, 50.0)] * 5,
    ),
    (
        # Same formula, compute slots instead of hours: 2 + 2 of 4 slots = 100%.
        "gpu_slots_same_formula",
        4.0,
        ResourceKind.gpu,
        [(2.0, D(2026, 6, 1), D(2026, 6, 2)), (2.0, D(2026, 6, 1), D(2026, 6, 2))],
        D(2026, 6, 1),
        D(2026, 6, 2),
        [(4.0, 100.0), (4.0, 100.0)],
    ),
    (
        "unscheduled_task_ignored",
        8.0,
        ResourceKind.human,
        [(5.0, None, None)],
        D(2026, 6, 1),
        D(2026, 6, 2),
        [(0.0, 0.0), (0.0, 0.0)],
    ),
]


@pytest.mark.parametrize(
    "name,capacity,kind,specs,start,end,expected", CASES, ids=[c[0] for c in CASES]
)
def test_compute_utilization_table(name, capacity, kind, specs, start, end, expected):
    resource = _mk_resource(capacity, kind)
    tasks = _mk_tasks(specs)
    result = compute_utilization(resource, tasks, start, end)

    assert len(result) == len(expected)
    expected_day = start
    for row, (exp_committed, exp_util) in zip(result, expected):
        assert row.day == expected_day
        assert row.committed == pytest.approx(exp_committed)
        # For compute resources capacity is fixed; for human resources it is
        # derived per-day (8.0 on weekdays for 09:00-17:00, 0 on weekends).
        if kind != ResourceKind.human:
            assert row.capacity == pytest.approx(capacity)
        assert row.utilization == pytest.approx(exp_util)
        expected_day += timedelta(days=1)


def test_compute_utilization_filters_by_resource():
    # Jun 1 2026 = Monday; 09:00-17:00 → capacity 8 h
    resource = _mk_resource(rid=1)
    mine = Task(
        title="mine", project_id=1, resource_id=1, load=4.0, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1)
    )
    other = Task(
        title="other", project_id=1, resource_id=2, load=8.0, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1)
    )
    result = compute_utilization(resource, [mine, other], D(2026, 6, 1), D(2026, 6, 1))
    assert result[0].committed == 4.0
    assert result[0].utilization == pytest.approx(50.0)


def test_compute_utilization_inverted_range_is_empty():
    resource = _mk_resource()
    assert compute_utilization(resource, [], D(2026, 6, 5), D(2026, 6, 1)) == []


# --- resource_capacity_on_day -------------------------------------------------


def test_capacity_on_weekday():
    r = _mk_resource(available_from="09:00", available_to="17:00", available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(8.0)  # Monday


def test_capacity_on_weekend_is_zero():
    r = _mk_resource(available_from="09:00", available_to="17:00", available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 6)) == pytest.approx(0.0)  # Saturday


def test_capacity_with_custom_hours():
    r = _mk_resource(available_from="08:30", available_to="16:30", available_days=31)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(8.0)


def test_capacity_partial_week():
    # Only Mon+Tue (bits 0+1 = 3)
    r = _mk_resource(available_from="09:00", available_to="13:00", available_days=3)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(4.0)  # Monday
    assert resource_capacity_on_day(r, D(2026, 6, 3)) == pytest.approx(0.0)  # Wednesday


def test_capacity_compute_resource():
    r = _mk_resource(capacity=4.0, kind=ResourceKind.gpu)
    assert resource_capacity_on_day(r, D(2026, 6, 1)) == pytest.approx(4.0)
    assert resource_capacity_on_day(r, D(2026, 6, 6)) == pytest.approx(4.0)  # Sat still 4 slots


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
    # Jun 1-3 2026 = Mon-Wed; 09:00-17:00 Mon-Fri gives 8 h/day capacity
    resource = Resource(name="Alice", kind=ResourceKind.human,
                        available_from="09:00", available_to="17:00", available_days=31)
    session.add_all([project, resource])
    session.commit()
    session.add(
        Task(
            title="t",
            project_id=project.id,
            resource_id=resource.id,
            load=4.0,
            start_date=D(2026, 6, 1),
            end_date=D(2026, 6, 2),
        )
    )
    session.commit()

    sched = resource_schedule(session, resource.id, D(2026, 6, 1), D(2026, 6, 3))
    assert [r.utilization for r in sched] == pytest.approx([50.0, 50.0, 0.0])


def test_resource_schedule_missing_resource_returns_none(session):
    assert resource_schedule(session, 9999, D(2026, 6, 1), D(2026, 6, 2)) is None
