from datetime import date

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Project, Resource, ResourceKind, Task, TaskResource
from app.scheduling import detect_conflicts, resource_conflicts

D = date


def _mk_resource(kind=ResourceKind.human, rid=1, available_from="09:00",
                 available_to="17:00", available_days=31):
    r = Resource(name="r", kind=kind, available_from=available_from,
                 available_to=available_to, available_days=available_days)
    r.id = rid
    return r


def _task(title, start, end):
    return Task(title=title, project_id=1, start_date=start, end_date=end)


def test_two_concurrent_tasks_flags_conflict():
    # 2 tasks → committed=2, capacity=1 → conflict on both days
    res = _mk_resource()
    tasks = [
        _task("A", D(2026, 6, 1), D(2026, 6, 2)),
        _task("B", D(2026, 6, 1), D(2026, 6, 2)),
    ]
    conflicts = detect_conflicts(res, tasks, D(2026, 6, 1), D(2026, 6, 2))
    assert [c.day for c in conflicts] == [D(2026, 6, 1), D(2026, 6, 2)]
    for c in conflicts:
        assert c.committed == pytest.approx(2.0)
        assert c.capacity == pytest.approx(1.0)
        assert c.overage == pytest.approx(1.0)
        assert {t.title for t in c.tasks} == {"A", "B"}


def test_one_task_is_not_a_conflict():
    # 1 task = fully booked (100%) — NOT a conflict
    res = _mk_resource()
    tasks = [_task("A", D(2026, 6, 1), D(2026, 6, 2))]
    assert detect_conflicts(res, tasks, D(2026, 6, 1), D(2026, 6, 2)) == []


def test_only_conflict_days_flagged():
    # A alone on Jun 1 (no conflict); A+B on Jun 2 (conflict); A alone on Jun 3 (no conflict)
    res = _mk_resource()
    a = _task("A", D(2026, 6, 1), D(2026, 6, 3))
    b = _task("B", D(2026, 6, 2), D(2026, 6, 2))
    conflicts = detect_conflicts(res, [a, b], D(2026, 6, 1), D(2026, 6, 3))
    assert [x.day for x in conflicts] == [D(2026, 6, 2)]
    assert {t.title for t in conflicts[0].tasks} == {"A", "B"}
    assert conflicts[0].overage == pytest.approx(1.0)


def test_three_concurrent_tasks_overage():
    res = _mk_resource()
    tasks = [
        _task("A", D(2026, 6, 1), D(2026, 6, 1)),
        _task("B", D(2026, 6, 1), D(2026, 6, 1)),
        _task("C", D(2026, 6, 1), D(2026, 6, 1)),
    ]
    conflicts = detect_conflicts(res, tasks, D(2026, 6, 1), D(2026, 6, 1))
    assert len(conflicts) == 1
    assert conflicts[0].committed == pytest.approx(3.0)
    assert conflicts[0].overage == pytest.approx(2.0)
    assert {t.title for t in conflicts[0].tasks} == {"A", "B", "C"}


def test_no_conflicts_single_task():
    res = _mk_resource()
    tasks = [_task("A", D(2026, 6, 1), D(2026, 6, 3))]
    assert detect_conflicts(res, tasks, D(2026, 6, 1), D(2026, 6, 3)) == []


def test_gpu_all_week_two_tasks_conflict():
    # GPU available every day; 2 concurrent tasks → conflict
    gpu = _mk_resource(kind=ResourceKind.gpu, available_days=127)
    tasks = [
        _task("morning", D(2026, 6, 1), D(2026, 6, 1)),
        _task("afternoon", D(2026, 6, 1), D(2026, 6, 1)),
    ]
    conflicts = detect_conflicts(gpu, tasks, D(2026, 6, 1), D(2026, 6, 1))
    assert len(conflicts) == 1
    assert conflicts[0].committed == pytest.approx(2.0)
    assert conflicts[0].overage == pytest.approx(1.0)


def test_contributing_tasks_exclude_nonoverlapping():
    res = _mk_resource(rid=1)
    a = _task("A", D(2026, 6, 2), D(2026, 6, 2))
    b = _task("B", D(2026, 6, 2), D(2026, 6, 2))
    earlier = _task("earlier", D(2026, 6, 1), D(2026, 6, 1))
    conflicts = detect_conflicts(res, [a, b, earlier], D(2026, 6, 2), D(2026, 6, 2))
    assert len(conflicts) == 1
    assert {t.title for t in conflicts[0].tasks} == {"A", "B"}


def test_task_on_unavailable_day_is_conflict():
    # Resource only Mon-Fri (available_days=31); Saturday task → conflict
    res = _mk_resource(available_days=31)
    tasks = [_task("weekend", D(2026, 6, 6), D(2026, 6, 6))]  # Saturday
    conflicts = detect_conflicts(res, tasks, D(2026, 6, 6), D(2026, 6, 6))
    assert len(conflicts) == 1
    assert conflicts[0].capacity == pytest.approx(0.0)
    assert conflicts[0].committed == pytest.approx(1.0)


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
                   available_from="00:00", available_to="23:59", available_days=127)
    session.add_all([project, gpu])
    session.commit()
    tasks = [
        Task(title="A", project_id=project.id, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1)),
        Task(title="B", project_id=project.id, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1)),
        Task(title="C", project_id=project.id, start_date=D(2026, 6, 1), end_date=D(2026, 6, 1)),
    ]
    session.add_all(tasks)
    session.flush()
    session.add_all([TaskResource(task_id=t.id, resource_id=gpu.id) for t in tasks])
    session.commit()

    conflicts = resource_conflicts(session, gpu.id, D(2026, 6, 1), D(2026, 6, 2))
    assert len(conflicts) == 1
    assert conflicts[0].day == D(2026, 6, 1)
    assert {t.title for t in conflicts[0].tasks} == {"A", "B", "C"}


def test_resource_conflicts_missing_resource_returns_none(session):
    assert resource_conflicts(session, 9999, D(2026, 6, 1), D(2026, 6, 2)) is None
