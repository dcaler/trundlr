"""Pools: a resource whose tasks run on whichever member lane is free first.

A pool has no runner. Each member's runner also claims the pool's tasks, and a
claim moves the task onto that lane (remembering the pool, so a retry goes back
to it). Re-flow plans a pool task on the member that opens up first.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_fk_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session):
    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


_ALWAYS = {"available_from": "00:00", "available_to": "23:59", "available_days": 127}


def _res(client, name, kind="gpu", **links):
    resp = client.post("/api/resources/", json={"name": name, "kind": kind, **_ALWAYS, **links})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture(name="lanes")
def lanes_fixture(client):
    """GPU 0, GPU 1, the dual lane spanning both, and a pool of the two singles."""
    gpu0 = _res(client, "GPU 0")
    gpu1 = _res(client, "GPU 1")
    dual = _res(client, "GPU 0+1", spans_ids=[gpu0, gpu1])
    pool = _res(client, "GPU (any)", member_ids=[gpu0, gpu1])
    client.patch("/api/settings/", json={"lane_settle_minutes": 0})
    return gpu0, gpu1, dual, pool


@pytest.fixture(name="project_id")
def project_id_fixture(client):
    return client.post("/api/projects/", json={"name": "P"}).json()["id"]


def _task(client, project_id, rid, title="t", **extra):
    resp = client.post("/api/tasks/", json={
        "title": title, "project_id": project_id, "resource_ids": [rid],
        "command": "echo hi", **extra,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _claim(client, rid):
    return client.post(f"/api/runner/{rid}/claim")


def _get(client, task_id):
    return client.get(f"/api/tasks/{task_id}").json()


def _utcnow():
    # No settings row with a timezone → the app timezone is UTC.
    return datetime.utcnow().replace(microsecond=0)


# ── API ──────────────────────────────────────────────────────────────────────

def test_members_round_trip(client, lanes):
    gpu0, gpu1, _, pool = lanes
    assert client.get(f"/api/resources/{pool}").json()["member_ids"] == [gpu0, gpu1]
    client.patch(f"/api/resources/{pool}", json={"name": "Any GPU"})
    assert client.get(f"/api/resources/{pool}").json()["member_ids"] == [gpu0, gpu1]
    client.patch(f"/api/resources/{pool}", json={"member_ids": [gpu1]})
    assert client.get(f"/api/resources/{pool}").json()["member_ids"] == [gpu1]


def test_member_rules(client, lanes):
    gpu0, _, dual, pool = lanes
    human = _res(client, "Cale", kind="human")
    bad = [
        {"member_ids": [human]},           # not a cpu/gpu lane
        {"member_ids": [pool]},            # a pool
        {"member_ids": [dual]},            # a spanning lane: needs all its cards
        {"member_ids": [999]},             # missing
    ]
    for links in bad:
        resp = client.post("/api/resources/", json={"name": "X", "kind": "gpu", **links})
        assert resp.status_code in (404, 422), links
    assert client.patch(f"/api/resources/{pool}", json={"member_ids": [pool]}).status_code == 422
    assert client.patch(f"/api/resources/{pool}", json={"spans_ids": [gpu0]}).status_code == 422
    assert client.patch(f"/api/resources/{dual}", json={"spans_ids": [pool]}).status_code == 422
    assert client.patch(f"/api/resources/{gpu0}", json={"member_ids": [dual]}).status_code == 422


def test_deleting_a_member_or_the_pool_tidies_up(client, lanes, project_id):
    gpu0, gpu1, _, pool = lanes
    client.delete(f"/api/resources/{gpu1}")
    assert client.get(f"/api/resources/{pool}").json()["member_ids"] == [gpu0]
    t = _task(client, project_id, pool)
    _claim(client, gpu0)
    assert _get(client, t)["pool_id"] == pool
    assert client.delete(f"/api/resources/{pool}").status_code == 204
    assert _get(client, t)["pool_id"] is None


# ── Claiming ─────────────────────────────────────────────────────────────────

def test_a_pool_has_no_runner(client, lanes):
    *_, pool = lanes
    assert _claim(client, pool).status_code == 422


def test_whichever_card_asks_first_takes_the_pool_task(client, lanes, project_id):
    gpu0, gpu1, _, pool = lanes
    a = _task(client, project_id, pool, title="a")
    b = _task(client, project_id, pool, title="b")

    first = _claim(client, gpu1).json()
    assert first["id"] == a and first["resource_ids"] == [gpu1] and first["pool_id"] == pool
    # Both cards work in parallel on the one queue.
    second = _claim(client, gpu0).json()
    assert second["id"] == b and second["resource_ids"] == [gpu0]
    assert _get(client, a)["resource_ids"] == [gpu1]


def test_own_and_pool_tasks_share_one_queue_in_start_order(client, lanes, project_id):
    gpu0, _, _, pool = lanes
    soon = _utcnow() + timedelta(hours=1)
    own = _task(client, project_id, gpu0, title="own", duration=1)
    client.patch(f"/api/tasks/{own}", json={
        "start_date": soon.isoformat(), "end_date": (soon + timedelta(hours=1)).isoformat(),
        "pinned": True})
    pooled = _task(client, project_id, pool, title="pooled", duration=1)
    assert _claim(client, gpu0).json()["id"] == pooled
    client.patch(f"/api/tasks/{pooled}", json={"status": "done"})
    assert _claim(client, gpu0).json()["id"] == own


def test_retrying_puts_the_task_back_on_the_pool(client, lanes, project_id):
    gpu0, gpu1, _, pool = lanes
    t = _task(client, project_id, pool)
    _claim(client, gpu0)
    client.patch(f"/api/tasks/{t}", json={"status": "failed", "exit_code": 1})
    errored = _get(client, t)
    assert errored["status"] == "error" and errored["resource_ids"] == [gpu0]

    retried = client.patch(f"/api/tasks/{t}", json={"status": "todo"}).json()
    assert retried["resource_ids"] == [pool] and retried["pool_id"] is None
    assert _claim(client, gpu1).json()["id"] == t   # any member, not just the one that failed


def test_reassigning_by_hand_forgets_the_pool(client, lanes, project_id):
    gpu0, gpu1, _, pool = lanes
    t = _task(client, project_id, pool)
    _claim(client, gpu0)
    client.patch(f"/api/tasks/{t}", json={"status": "failed", "exit_code": 1})
    client.patch(f"/api/tasks/{t}", json={"resource_ids": [gpu1]})
    resp = client.patch(f"/api/tasks/{t}", json={"status": "todo"}).json()
    assert resp["resource_ids"] == [gpu1] and resp["pool_id"] is None


def test_a_due_dual_task_drains_pool_work_too(client, lanes, project_id):
    gpu0, gpu1, dual, pool = lanes
    running = _task(client, project_id, gpu0, title="gpu0 job", duration=5)
    assert _claim(client, gpu0).json()["id"] == running
    d = _task(client, project_id, dual)
    client.patch(f"/api/tasks/{d}", json={"start_date": "2020-01-01T00:00:00",
                                          "end_date": "2020-01-01T01:00:00", "pinned": True})
    _task(client, project_id, pool, title="pooled", duration=0.5)
    assert _claim(client, gpu1).headers["X-Runner-Idle"] == f"draining-for:{d}"


# ── Re-flow ──────────────────────────────────────────────────────────────────

def test_pool_tasks_are_planned_on_both_cards_at_once(client, lanes, project_id):
    *_, pool = lanes
    a = _task(client, project_id, pool, title="a", duration=3)
    b = _task(client, project_id, pool, title="b", duration=3)
    client.post("/api/schedule/reflow")
    assert _get(client, a)["start_date"] == _get(client, b)["start_date"]


def test_a_pool_task_is_planned_on_the_card_that_frees_first(client, lanes, project_id):
    gpu0, _, _, pool = lanes
    _task(client, project_id, gpu0, title="long gpu0 job", duration=10)
    _claim(client, gpu0)
    t = _task(client, project_id, pool, duration=1)
    start = datetime.fromisoformat(_get(client, t)["start_date"])
    assert start < _utcnow() + timedelta(minutes=5)   # GPU 1 is free now


def test_a_pinned_pool_task_holds_a_card_but_not_a_particular_one(client, lanes, project_id):
    gpu0, gpu1, _, pool = lanes
    now = _utcnow()
    pinned = _task(client, project_id, pool, title="pinned", duration=2)
    client.patch(f"/api/tasks/{pinned}", json={
        "start_date": now.isoformat(), "end_date": (now + timedelta(hours=2)).isoformat(),
        "pinned": True})
    on0 = _task(client, project_id, gpu0, title="gpu0 work", duration=1)
    on1 = _task(client, project_id, gpu1, title="gpu1 work", duration=1)
    client.post("/api/schedule/reflow")
    starts = sorted(datetime.fromisoformat(_get(client, t)["start_date"]) for t in (on0, on1))
    # One card is held for the pinned task; the other is free straight away.
    assert starts[0] < now + timedelta(minutes=5)
    assert starts[1] >= now + timedelta(hours=2) - timedelta(minutes=1)
