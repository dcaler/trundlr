"""Spanning lanes: a resource built from others (e.g. GPU 0+1 spans GPU 0 and
GPU 1) never runs alongside them — not at claim time, not in the reflow plan —
and its eligible tasks drain the lanes it spans so it is not starved.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db, get_engine
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


def _gpu(client, name, spans_ids=()):
    resp = client.post("/api/resources/", json={
        "name": name, "kind": "gpu", **_ALWAYS, "spans_ids": list(spans_ids),
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture(name="lanes")
def lanes_fixture(client):
    gpu0 = _gpu(client, "GPU 0")
    gpu1 = _gpu(client, "GPU 1")
    dual = _gpu(client, "GPU 0+1", spans_ids=[gpu0, gpu1])
    return gpu0, gpu1, dual


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


def _set_status(client, task_id, status):
    assert client.patch(f"/api/tasks/{task_id}", json={"status": status}).status_code == 200


# ── Spans API ────────────────────────────────────────────────────────────────

def test_spans_round_trip(client, lanes):
    gpu0, gpu1, dual = lanes
    assert client.get(f"/api/resources/{dual}").json()["spans_ids"] == [gpu0, gpu1]
    assert client.get(f"/api/resources/{gpu0}").json()["spans_ids"] == []
    listed = {r["id"]: r["spans_ids"] for r in client.get("/api/resources/").json()}
    assert listed[dual] == [gpu0, gpu1]


def test_patch_replaces_spans_and_leaves_them_when_omitted(client, lanes):
    gpu0, gpu1, dual = lanes
    client.patch(f"/api/resources/{dual}", json={"name": "Dual"})
    assert client.get(f"/api/resources/{dual}").json()["spans_ids"] == [gpu0, gpu1]
    client.patch(f"/api/resources/{dual}", json={"spans_ids": [gpu0]})
    assert client.get(f"/api/resources/{dual}").json()["spans_ids"] == [gpu0]


def test_cannot_span_self(client, lanes):
    gpu0, _, _ = lanes
    assert client.patch(f"/api/resources/{gpu0}", json={"spans_ids": [gpu0]}).status_code == 422


def test_cannot_span_missing_resource(client):
    resp = client.post("/api/resources/", json={"name": "X", "kind": "gpu", "spans_ids": [999]})
    assert resp.status_code == 404


def test_spans_are_one_level_deep(client, lanes):
    gpu0, _, dual = lanes
    # A spanning lane cannot itself be spanned...
    resp = client.post("/api/resources/", json={"name": "Y", "kind": "gpu", "spans_ids": [dual]})
    assert resp.status_code == 422
    # ...and a spanned lane cannot span others.
    other = _gpu(client, "GPU 2")
    assert client.patch(f"/api/resources/{gpu0}", json={"spans_ids": [other]}).status_code == 422


def test_deleting_a_resource_removes_its_spans(client, lanes):
    gpu0, gpu1, dual = lanes
    assert client.delete(f"/api/resources/{gpu0}").status_code == 204
    assert client.get(f"/api/resources/{dual}").json()["spans_ids"] == [gpu1]
    assert client.delete(f"/api/resources/{dual}").status_code == 204


# ── Claim exclusivity ────────────────────────────────────────────────────────

def test_single_lanes_run_in_parallel(client, lanes, project_id):
    gpu0, gpu1, _ = lanes
    _task(client, project_id, gpu0)
    _task(client, project_id, gpu1)
    assert _claim(client, gpu0).status_code == 200
    assert _claim(client, gpu1).status_code == 200


def test_dual_waits_while_a_single_lane_is_busy(client, lanes, project_id):
    gpu0, _, dual = lanes
    a = _task(client, project_id, gpu0)
    _task(client, project_id, dual)
    _set_status(client, a, "in_progress")
    resp = _claim(client, dual)
    assert resp.status_code == 204
    assert resp.headers["X-Runner-Idle"] == f"conflict-busy:{gpu0}:{a}"


def test_paused_task_also_holds_conflicting_lanes(client, lanes, project_id):
    gpu0, _, dual = lanes
    a = _task(client, project_id, gpu0)
    _task(client, project_id, dual)
    _set_status(client, a, "paused")
    assert _claim(client, dual).status_code == 204


def test_single_lanes_wait_while_dual_runs(client, lanes, project_id):
    gpu0, gpu1, dual = lanes
    d = _task(client, project_id, dual)
    _task(client, project_id, gpu0)
    _task(client, project_id, gpu1)
    _set_status(client, d, "in_progress")
    for rid in (gpu0, gpu1):
        resp = _claim(client, rid)
        assert resp.status_code == 204
        assert resp.headers["X-Runner-Idle"] == f"conflict-busy:{dual}:{d}"


def test_dual_claims_when_single_lanes_are_idle(client, lanes, project_id):
    _, _, dual = lanes
    d = _task(client, project_id, dual)
    resp = _claim(client, dual)
    assert resp.status_code == 200
    assert resp.json()["id"] == d


# ── Starvation ───────────────────────────────────────────────────────────────

def test_eligible_dual_task_drains_single_lanes(client, lanes, project_id):
    gpu0, gpu1, dual = lanes
    running = _task(client, project_id, gpu0, title="long gpu0 job")
    _set_status(client, running, "in_progress")
    d = _task(client, project_id, dual)
    # Pinned so the reflow on the next create keeps its (overdue) start time.
    client.patch(f"/api/tasks/{d}", json={"start_date": "2020-01-01T00:00:00",
                                          "end_date": "2020-01-01T01:00:00",
                                          "pinned": True})
    _task(client, project_id, gpu1, title="next gpu1 job")

    # GPU 1 is idle and has work, but must make way for the waiting dual task.
    resp = _claim(client, gpu1)
    assert resp.status_code == 204
    assert resp.headers["X-Runner-Idle"] == f"draining-for:{d}"

    # When GPU 0's job ends the dual task runs, then the single lanes resume.
    _set_status(client, running, "done")
    assert _claim(client, dual).json()["id"] == d
    assert _claim(client, gpu1).status_code == 204
    _set_status(client, d, "done")
    assert _claim(client, gpu1).status_code == 200


def test_dual_task_before_its_start_does_not_drain(client, lanes, project_id):
    gpu0, _, dual = lanes
    d = _task(client, project_id, dual)
    future = (datetime.now() + timedelta(days=30)).replace(microsecond=0)
    client.patch(f"/api/tasks/{d}", json={
        "start_date": future.isoformat(), "end_date": (future + timedelta(hours=1)).isoformat(),
        "pinned": True,
    })
    _task(client, project_id, gpu0)
    assert _claim(client, gpu0).status_code == 200


def test_dual_task_with_unmet_dependency_does_not_drain(client, lanes, project_id):
    gpu0, _, dual = lanes
    upstream = _task(client, project_id, gpu0, title="upstream")
    _task(client, project_id, dual, depends_on_id=upstream)
    assert _claim(client, gpu0).json()["id"] == upstream


def test_unrelated_lanes_are_unaffected(client, lanes, project_id):
    _, _, dual = lanes
    cpu = client.post("/api/resources/", json={"name": "CPU", "kind": "cpu", **_ALWAYS}).json()["id"]
    d = _task(client, project_id, dual)
    _set_status(client, d, "in_progress")
    _task(client, project_id, cpu)
    assert _claim(client, cpu).status_code == 200


# ── Reflow ───────────────────────────────────────────────────────────────────

def _interval(client, task_id):
    t = client.get(f"/api/tasks/{task_id}").json()
    return datetime.fromisoformat(t["start_date"]), datetime.fromisoformat(t["end_date"])


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def test_reflow_serialises_dual_against_single_lanes(client, lanes, project_id):
    gpu0, gpu1, dual = lanes
    a = _task(client, project_id, gpu0, duration=2)
    b = _task(client, project_id, gpu1, duration=2)
    d = _task(client, project_id, dual, duration=2)
    assert client.post("/api/schedule/reflow").status_code == 200

    ia, ib, idual = _interval(client, a), _interval(client, b), _interval(client, d)
    assert _overlap(ia, ib)          # single lanes share no hardware
    assert not _overlap(idual, ia)   # the dual lane uses both cards
    assert not _overlap(idual, ib)


def test_reflow_routes_single_lane_work_around_running_dual(client, lanes, project_id):
    gpu0, _, dual = lanes
    d = _task(client, project_id, dual, duration=3)
    assert _claim(client, dual).status_code == 200  # now in_progress, fixed in place
    a = _task(client, project_id, gpu0, duration=1)
    client.post("/api/schedule/reflow")
    assert not _overlap(_interval(client, a), _interval(client, d))


# ── SQLite concurrency ───────────────────────────────────────────────────────

def test_file_database_uses_wal_so_reads_pass_a_pending_write(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        c.execute(text("CREATE TABLE t (x INTEGER)"))
        c.commit()

    with engine.connect() as writer, engine.connect() as reader:
        writer.execute(text("BEGIN IMMEDIATE"))
        writer.execute(text("INSERT INTO t VALUES (1)"))
        # In the rollback journal this read would wait on the writer's lock.
        assert reader.execute(text("SELECT count(*) FROM t")).scalar() == 0
        writer.execute(text("COMMIT"))
