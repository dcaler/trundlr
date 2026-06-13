"""Unit tests for the priority-driven re-flow scheduler (reflow_schedule).

Covers the behaviours that motivated the rewrite: cross-resource dependencies,
project-priority ordering, gap backfilling, idempotency, pinned obstacles, and
the no-bogus-date guard (unschedulable tasks are left untouched and reported).
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app
from app.models import Project, Resource, ResourceKind, Task, TaskResource, TaskStatus
from app.scheduling import reflow_schedule


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _project(s, priority=3, name="P"):
    p = Project(name=name, priority=priority)
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _resource(s, name="R", kind=ResourceKind.human, days=127,
              frm="00:00", to="23:59"):
    # Wide-open availability by default so placement is driven by deps/priority,
    # not by window edges. days=127 = all seven days.
    r = Resource(name=name, kind=kind, available_from=frm, available_to=to,
                 available_days=days)
    s.add(r)
    s.commit()
    s.refresh(r)
    return r


def _task(s, project, resources, *, status=TaskStatus.todo, duration=1.0,
          depends_on=None, pinned=False, start=None, end=None, title="t"):
    t = Task(title=title, project_id=project.id, status=status, duration=duration,
             depends_on_id=depends_on, pinned=pinned, start_date=start, end_date=end)
    s.add(t)
    s.commit()
    s.refresh(t)
    for r in resources:
        s.add(TaskResource(task_id=t.id, resource_id=r.id))
    s.commit()
    return t


# ── cross-resource dependency (the screenshot bug) ──────────────────────────

def test_dependency_observed_across_resources(session):
    p = _project(session, priority=2)
    cale = _resource(session, name="Cale")
    gpu = _resource(session, name="GPU", kind=ResourceKind.gpu)

    write = _task(session, p, [gpu], duration=2.0, title="write")
    comment = _task(session, p, [cale], duration=1.0, depends_on=write.id,
                    title="comment")

    reflow_schedule(session)
    session.commit()

    assert write.start_date is not None and comment.start_date is not None
    # The dependent must not start before its prerequisite finishes — even though
    # they live on different resources.
    assert comment.start_date >= write.end_date


# ── project-priority ordering ───────────────────────────────────────────────

def test_higher_priority_project_scheduled_first(session):
    hi = _project(session, priority=1, name="Hi")
    lo = _project(session, priority=4, name="Lo")
    r = _resource(session, name="R")

    t_lo = _task(session, lo, [r], duration=1.0, title="lo")
    t_hi = _task(session, hi, [r], duration=1.0, title="hi")

    reflow_schedule(session)
    session.commit()

    assert t_hi.start_date < t_lo.start_date


# ── backfill: a low-priority free task fills the gap a blocked high-priority
#    task leaves behind ───────────────────────────────────────────────────────

def test_low_priority_task_backfills_gap(session):
    hi = _project(session, priority=1, name="Hi")
    lo = _project(session, priority=4, name="Lo")
    a = _resource(session, name="A")
    b = _resource(session, name="B")

    pre = _task(session, hi, [b], duration=5.0, title="pre")          # on B
    dependent = _task(session, hi, [a], duration=1.0, depends_on=pre.id,
                      title="dependent")                               # on A, waits 5h
    filler = _task(session, lo, [a], duration=1.0, title="filler")     # on A, free

    reflow_schedule(session)
    session.commit()

    # The high-priority dependent can't start until `pre` ends, so the low-priority
    # filler claims the earlier slot on A despite its lower priority.
    assert filler.start_date < dependent.start_date
    assert dependent.start_date >= pre.end_date


# ── no-bogus-date guard ─────────────────────────────────────────────────────

def test_blocked_dependency_leaves_task_unscheduled(session):
    p = _project(session)
    r = _resource(session, name="R")
    blocker = _task(session, p, [r], status=TaskStatus.blocked, title="blocker")
    waiting = _task(session, p, [r], depends_on=blocker.id, title="waiting")

    result = reflow_schedule(session)
    session.commit()

    assert waiting.start_date is None  # never stamped with a date
    assert any(u["id"] == waiting.id for u in result["unscheduled"])


def test_no_availability_leaves_task_unscheduled(session):
    p = _project(session)
    # available_days=0 → resource is never available, so nothing fits.
    r = _resource(session, name="Idle", days=0)
    t = _task(session, p, [r], title="orphan")

    result = reflow_schedule(session)
    session.commit()

    assert t.start_date is None
    reasons = [u["reason"] for u in result["unscheduled"] if u["id"] == t.id]
    assert reasons and "availability" in reasons[0]


# ── idempotency ─────────────────────────────────────────────────────────────

def test_reflow_is_idempotent(session):
    p = _project(session, priority=2)
    r = _resource(session, name="R")
    _task(session, p, [r], duration=1.0, title="a")
    _task(session, p, [r], duration=1.0, title="b")

    reflow_schedule(session)
    session.commit()
    second = reflow_schedule(session)
    session.commit()

    assert second["changed"] == 0


# ── pinned tasks act as immovable obstacles ─────────────────────────────────

def test_pinned_task_is_preserved_and_blocks_slot(session):
    from datetime import datetime

    p = _project(session, priority=1)
    r = _resource(session, name="R")
    # A pinned todo occupying a fixed 2-hour slot well in the future.
    pin_start = datetime(2099, 1, 1, 10, 0)
    pin_end = datetime(2099, 1, 1, 12, 0)
    pinned = _task(session, p, [r], duration=2.0, pinned=True,
                   start=pin_start, end=pin_end, title="pinned")
    mover = _task(session, p, [r], duration=2.0, title="mover")

    result = reflow_schedule(session)
    session.commit()

    # Pinned task keeps its dates and is counted; the mover does not overlap it.
    assert pinned.start_date == pin_start and pinned.end_date == pin_end
    assert result["pinned"] == 1
    assert not (mover.start_date < pin_end and mover.end_date > pin_start)


# ── endpoint wiring ─────────────────────────────────────────────────────────

def test_reflow_endpoint(session):
    """POST /api/schedule/reflow places tasks and persists them via the route."""
    p = _project(session, priority=1)
    r = _resource(session, name="R")
    t = _task(session, p, [r], duration=1.0, title="endpoint")

    engine = session.get_bind()

    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    try:
        resp = TestClient(app).post("/api/schedule/reflow")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] == 1
    assert body["unscheduled"] == []
    session.refresh(t)
    assert t.start_date is not None
