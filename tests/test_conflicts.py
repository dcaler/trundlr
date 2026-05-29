from datetime import date

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Project, Resource, ResourceKind, Task
from app.scheduling import detect_conflicts, resource_conflicts

D = date


def _mk_resource(capacity=None, kind=ResourceKind.gpu, rid=1):
    if kind == ResourceKind.human:
        r = Resource(name="r", kind=kind, available_from="09:00",
                     available_to="17:00", available_days=31)
    else:
        r = Resource(name="r", kind=kind, capacity=capacity)
    r.id = rid
    return r


def _task(title, load, start, end, rid=1):
    return Task(title=title, project_id=1, resource_id=rid, load=load, start_date=start, end_date=end)


def test_overbooked_gpu_flags_days_and_contributing_tasks():
    # Plan's core case: 3 tasks needing 2 slots each on a 4-slot node.
    gpu = _mk_resource(4.0)
    tasks = [
        _task("A", 2.0, D(2026, 6, 1), D(2026, 6, 2)),
        _task("B", 2.0, D(2026, 6, 1), D(2026, 6, 2)),
        _task("C", 2.0, D(2026, 6, 1), D(2026, 6, 2)),
    ]
    conflicts = detect_conflicts(gpu, tasks, D(2026, 6, 1), D(2026, 6, 2))

    assert [c.day for c in conflicts] == [D(2026, 6, 1), D(2026, 6, 2)]
    for c in conflicts:
        assert c.committed == pytest.approx(6.0)
        assert c.capacity == 4.0
        assert c.overage == pytest.approx(2.0)
        assert {t.title for t in c.tasks} == {"A", "B", "C"}


def test_fully_booked_is_not_flagged():
    # Off-by-one guard: committed == capacity must NOT be a conflict.
    gpu = _mk_resource(4.0)
    tasks = [
        _task("A", 2.0, D(2026, 6, 1), D(2026, 6, 2)),
        _task("B", 2.0, D(2026, 6, 1), D(2026, 6, 2)),
    ]
    assert detect_conflicts(gpu, tasks, D(2026, 6, 1), D(2026, 6, 2)) == []


def test_only_overbooked_days_are_flagged():
    # A+B fill the node exactly on Jun 1 & 3; C pushes Jun 2 over.
    gpu = _mk_resource(4.0)
    a = _task("A", 2.0, D(2026, 6, 1), D(2026, 6, 3))
    b = _task("B", 2.0, D(2026, 6, 1), D(2026, 6, 3))
    c = _task("C", 2.0, D(2026, 6, 2), D(2026, 6, 2))
    conflicts = detect_conflicts(gpu, [a, b, c], D(2026, 6, 1), D(2026, 6, 3))

    assert [x.day for x in conflicts] == [D(2026, 6, 2)]
    assert {t.title for t in conflicts[0].tasks} == {"A", "B", "C"}
    assert conflicts[0].overage == pytest.approx(2.0)


def test_barely_over_is_flagged():
    # Strict > also catches a small overage from the other side of the boundary.
    gpu = _mk_resource(4.0)
    tasks = [
        _task("A", 2.0, D(2026, 6, 1), D(2026, 6, 1)),
        _task("B", 2.0, D(2026, 6, 1), D(2026, 6, 1)),
        _task("C", 0.5, D(2026, 6, 1), D(2026, 6, 1)),
    ]
    conflicts = detect_conflicts(gpu, tasks, D(2026, 6, 1), D(2026, 6, 1))
    assert len(conflicts) == 1
    assert conflicts[0].overage == pytest.approx(0.5)


def test_no_conflicts_under_capacity():
    gpu = _mk_resource(4.0)
    tasks = [_task("A", 1.0, D(2026, 6, 1), D(2026, 6, 3))]
    assert detect_conflicts(gpu, tasks, D(2026, 6, 1), D(2026, 6, 3)) == []


def test_human_hours_over_allocation_is_kind_agnostic():
    # Same algorithm flags an over-booked human (hours) as a compute node (slots).
    # Jun 1 2026 = Monday; 09:00-17:00 availability gives 8 h capacity.
    human = _mk_resource(kind=ResourceKind.human)
    tasks = [
        _task("morning", 5.0, D(2026, 6, 1), D(2026, 6, 1)),
        _task("afternoon", 5.0, D(2026, 6, 1), D(2026, 6, 1)),
    ]
    conflicts = detect_conflicts(human, tasks, D(2026, 6, 1), D(2026, 6, 1))
    assert len(conflicts) == 1
    assert conflicts[0].committed == pytest.approx(10.0)
    assert conflicts[0].overage == pytest.approx(2.0)


def test_contributing_tasks_exclude_nonoverlapping_and_other_resources():
    gpu = _mk_resource(4.0, rid=1)
    a = _task("A", 3.0, D(2026, 6, 2), D(2026, 6, 2))
    b = _task("B", 3.0, D(2026, 6, 2), D(2026, 6, 2))
    earlier = _task("earlier", 4.0, D(2026, 6, 1), D(2026, 6, 1))  # different day
    other_res = _task("other", 4.0, D(2026, 6, 2), D(2026, 6, 2), rid=2)  # different resource
    conflicts = detect_conflicts(gpu, [a, b, earlier, other_res], D(2026, 6, 2), D(2026, 6, 2))

    assert len(conflicts) == 1
    assert {t.title for t in conflicts[0].tasks} == {"A", "B"}


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
    gpu = Resource(name="dgx", kind=ResourceKind.gpu, capacity=4.0)
    session.add_all([project, gpu])
    session.commit()
    session.add_all(
        [
            Task(title="A", project_id=project.id, resource_id=gpu.id, load=2.0,
                 start_date=D(2026, 6, 1), end_date=D(2026, 6, 1)),
            Task(title="B", project_id=project.id, resource_id=gpu.id, load=2.0,
                 start_date=D(2026, 6, 1), end_date=D(2026, 6, 1)),
            Task(title="C", project_id=project.id, resource_id=gpu.id, load=2.0,
                 start_date=D(2026, 6, 1), end_date=D(2026, 6, 1)),
        ]
    )
    session.commit()

    conflicts = resource_conflicts(session, gpu.id, D(2026, 6, 1), D(2026, 6, 2))
    assert len(conflicts) == 1
    assert conflicts[0].day == D(2026, 6, 1)
    assert {t.title for t in conflicts[0].tasks} == {"A", "B", "C"}


def test_resource_conflicts_missing_resource_returns_none(session):
    assert resource_conflicts(session, 9999, D(2026, 6, 1), D(2026, 6, 2)) is None
