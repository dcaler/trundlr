"""Conflict detection for the hours-based engine.

A day is a conflict when assigned task-hours exceed available hours.
"""

from datetime import date, datetime

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Project, Resource, ResourceKind, Task, TaskResource
from app.scheduling import detect_conflicts, resource_conflicts

D = date


def _dt(y, mo, d, h=0, m=0):
    return datetime(y, mo, d, h, m)


def _mk_resource(kind=ResourceKind.human, rid=1, available_from="09:00",
                 available_to="17:00", available_days=31):
    r = Resource(name="r", kind=kind, available_from=available_from,
                 available_to=available_to, available_days=available_days)
    r.id = rid
    return r


def _task(title, start, end):
    return Task(title=title, project_id=1, start_date=start, end_date=end)


def test_two_full_day_tasks_flag_conflict():
    # Two 8h tasks on an 8h day → committed 16 > capacity 8.
    res = _mk_resource()
    tasks = [
        _task("A", _dt(2026, 6, 1, 9), _dt(2026, 6, 1, 17)),
        _task("B", _dt(2026, 6, 1, 9), _dt(2026, 6, 1, 17)),
    ]
    conflicts = detect_conflicts(res, tasks, D(2026, 6, 1), D(2026, 6, 1))
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.committed == pytest.approx(16.0)
    assert c.capacity == pytest.approx(8.0)
    assert c.overage == pytest.approx(8.0)
    assert {t.title for t in c.tasks} == {"A", "B"}


def test_single_task_at_capacity_is_not_a_conflict():
    res = _mk_resource()
    tasks = [_task("A", _dt(2026, 6, 1, 9), _dt(2026, 6, 1, 17))]  # exactly 8h
    assert detect_conflicts(res, tasks, D(2026, 6, 1), D(2026, 6, 1)) == []


def test_under_capacity_is_not_a_conflict():
    res = _mk_resource()
    tasks = [_task("A", _dt(2026, 6, 1, 9), _dt(2026, 6, 1, 13))]  # 4h
    assert detect_conflicts(res, tasks, D(2026, 6, 1), D(2026, 6, 1)) == []


def test_only_over_days_flagged():
    res = _mk_resource()
    # Jun 1: one 8h task (at capacity). Jun 2: two 8h tasks (over).
    a = _task("A", _dt(2026, 6, 1, 9), _dt(2026, 6, 1, 17))
    b = _task("B", _dt(2026, 6, 2, 9), _dt(2026, 6, 2, 17))
    c = _task("C", _dt(2026, 6, 2, 9), _dt(2026, 6, 2, 17))
    conflicts = detect_conflicts(res, [a, b, c], D(2026, 6, 1), D(2026, 6, 2))
    assert [x.day for x in conflicts] == [D(2026, 6, 2)]
    assert {t.title for t in conflicts[0].tasks} == {"B", "C"}


def test_contributing_tasks_exclude_nonoverlapping():
    res = _mk_resource()
    a = _task("A", _dt(2026, 6, 2, 9), _dt(2026, 6, 2, 17))
    b = _task("B", _dt(2026, 6, 2, 9), _dt(2026, 6, 2, 17))
    earlier = _task("earlier", _dt(2026, 6, 1, 9), _dt(2026, 6, 1, 17))
    conflicts = detect_conflicts(res, [a, b, earlier], D(2026, 6, 2), D(2026, 6, 2))
    assert len(conflicts) == 1
    assert {t.title for t in conflicts[0].tasks} == {"A", "B"}


def test_task_on_unavailable_day_is_conflict():
    # Resource only Mon-Fri; a Saturday task → capacity 0, committed > 0.
    res = _mk_resource(available_days=31)
    tasks = [_task("weekend", _dt(2026, 6, 6, 9), _dt(2026, 6, 6, 12))]  # Saturday, 3h
    conflicts = detect_conflicts(res, tasks, D(2026, 6, 6), D(2026, 6, 6))
    assert len(conflicts) == 1
    assert conflicts[0].capacity == pytest.approx(0.0)
    assert conflicts[0].committed == pytest.approx(3.0)
    assert conflicts[0].overage == pytest.approx(3.0)


# --- DB entrypoint ------------------------------------------------------------


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


def test_resource_conflicts_db(session):
    project = Project(name="P")
    gpu = Resource(name="dgx", kind=ResourceKind.gpu,
                   available_from="09:00", available_to="17:00", available_days=127)
    session.add_all([project, gpu])
    session.commit()
    tasks = [
        Task(title="A", project_id=project.id, start_date=_dt(2026, 6, 1, 9), end_date=_dt(2026, 6, 1, 17)),
        Task(title="B", project_id=project.id, start_date=_dt(2026, 6, 1, 9), end_date=_dt(2026, 6, 1, 17)),
    ]
    session.add_all(tasks)
    session.flush()
    session.add_all([TaskResource(task_id=t.id, resource_id=gpu.id) for t in tasks])
    session.commit()

    conflicts = resource_conflicts(session, gpu.id, D(2026, 6, 1), D(2026, 6, 2))
    assert len(conflicts) == 1
    assert conflicts[0].day == D(2026, 6, 1)
    assert {t.title for t in conflicts[0].tasks} == {"A", "B"}


def test_resource_conflicts_missing_resource_returns_none(session):
    assert resource_conflicts(session, 9999, D(2026, 6, 1), D(2026, 6, 2)) is None
