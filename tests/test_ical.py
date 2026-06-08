from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app

HUMAN = {"name": "Alice", "kind": "human", "available_from": "09:00",
         "available_to": "17:00", "available_days": 31}


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


def test_ical_404_for_missing_resource(client):
    resp = client.get("/api/resources/9999/calendar.ics")
    assert resp.status_code == 404


def test_ical_empty_calendar(client):
    resource = client.post("/api/resources/", json=HUMAN).json()
    resp = client.get(f"/api/resources/{resource['id']}/calendar.ics")
    assert resp.status_code == 200
    assert "text/calendar" in resp.headers["content-type"]
    body = resp.text
    assert "BEGIN:VCALENDAR" in body
    assert "BEGIN:VEVENT" not in body


def test_ical_includes_scheduled_task(client):
    project = client.post("/api/projects/", json={"name": "P"}).json()
    resource = client.post("/api/resources/", json=HUMAN).json()
    client.post("/api/tasks/", json={
        "title": "Deploy Backend",
        "project_id": project["id"],
        "resource_ids": [resource["id"]],
        "start_date": "2026-06-01T09:00:00",
        "end_date": "2026-06-03T17:00:00",
    })

    resp = client.get(f"/api/resources/{resource['id']}/calendar.ics")
    assert resp.status_code == 200
    body = resp.text
    assert "BEGIN:VEVENT" in body
    assert "Deploy Backend" in body
    assert "DTSTART" in body
    assert "DTEND" in body


def test_ical_unscheduled_task_excluded(client):
    project = client.post("/api/projects/", json={"name": "P"}).json()
    resource = client.post("/api/resources/", json=HUMAN).json()
    client.post("/api/tasks/", json={
        "title": "Not Yet Scheduled",
        "project_id": project["id"],
        "resource_ids": [resource["id"]],
    })

    resp = client.get(f"/api/resources/{resource['id']}/calendar.ics")
    assert resp.status_code == 200
    assert "Not Yet Scheduled" not in resp.text


def test_ical_calendar_name_matches_resource(client):
    resource = client.post("/api/resources/", json=HUMAN).json()
    resp = client.get(f"/api/resources/{resource['id']}/calendar.ics")
    assert resp.status_code == 200
    assert "Alice" in resp.text


def test_ical_task_without_end_date(client):
    project = client.post("/api/projects/", json={"name": "P"}).json()
    resource = client.post("/api/resources/", json=HUMAN).json()
    client.post("/api/tasks/", json={
        "title": "Open Task",
        "project_id": project["id"],
        "resource_ids": [resource["id"]],
        "start_date": "2026-06-01T09:00:00",
    })

    resp = client.get(f"/api/resources/{resource['id']}/calendar.ics")
    assert resp.status_code == 200
    # Should be included; end defaults to start + 1 hour
    assert "Open Task" in resp.text
    assert "DTEND" in resp.text
