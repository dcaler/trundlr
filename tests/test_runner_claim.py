"""Tests for the /api/runner/{resource_id}/claim endpoint.

Regression coverage for a bug where the runner claimed a task on a GPU
resource even though an active ResourceBlockout/ResourceCalBlock covered
the current moment — the claim query never consulted blocks at all.
"""

from datetime import date, datetime, timedelta

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


@pytest.fixture(name="project_id")
def project_id_fixture(client):
    return client.post("/api/projects/", json={"name": "Test Project"}).json()["id"]


@pytest.fixture(name="gpu_id")
def gpu_id_fixture(client):
    resp = client.post("/api/resources/", json={
        "name": "GPU Node", "kind": "gpu",
        "available_from": "00:00", "available_to": "23:59", "available_days": 127,
    })
    return resp.json()["id"]


def _make_runnable_task(client, project_id, gpu_id):
    resp = client.post("/api/tasks/", json={
        "title": "Train model",
        "project_id": project_id,
        "resource_ids": [gpu_id],
        "command": "echo hi",
    })
    assert resp.status_code == 201
    return resp.json()["id"]


def test_claim_succeeds_with_no_block(client, project_id, gpu_id):
    _make_runnable_task(client, project_id, gpu_id)
    resp = client.post(f"/api/runner/{gpu_id}/claim")
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


def test_claim_refused_during_full_day_blockout(client, project_id, gpu_id):
    _make_runnable_task(client, project_id, gpu_id)
    today = date.today()
    resp = client.post(f"/api/resources/{gpu_id}/blockouts", json={
        "start_date": today.isoformat(),
        "end_date": today.isoformat(),
    })
    assert resp.status_code == 201

    claim = client.post(f"/api/runner/{gpu_id}/claim")
    assert claim.status_code == 204
    assert claim.headers.get("X-Runner-Idle") == "resource-blocked"


def test_claim_refused_during_partial_blockout_covering_now(client, project_id, gpu_id):
    _make_runnable_task(client, project_id, gpu_id)
    today = date.today()
    resp = client.post(f"/api/resources/{gpu_id}/blockouts", json={
        "start_date": today.isoformat(),
        "end_date": today.isoformat(),
        "from_time": "00:00",
        "to_time": "23:59",
    })
    assert resp.status_code == 201

    claim = client.post(f"/api/runner/{gpu_id}/claim")
    assert claim.status_code == 204
    assert claim.headers.get("X-Runner-Idle") == "resource-blocked"


def test_claim_succeeds_once_block_window_has_passed(client, project_id, gpu_id):
    _make_runnable_task(client, project_id, gpu_id)
    yesterday = date.today() - timedelta(days=1)
    resp = client.post(f"/api/resources/{gpu_id}/blockouts", json={
        "start_date": yesterday.isoformat(),
        "end_date": yesterday.isoformat(),
    })
    assert resp.status_code == 201

    claim = client.post(f"/api/runner/{gpu_id}/claim")
    assert claim.status_code == 200
