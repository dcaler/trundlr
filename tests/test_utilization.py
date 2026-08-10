"""Integration tests for the hours-based utilization dashboard.

Capacity = available hours/day; committed = assigned task-hours.
A day is over-allocated (a conflict) when committed > capacity.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── helpers ───────────────────────────────────────────────────────────────────

def mk_project(c):
    return c.post("/api/projects/", json={"name": "P"}).json()

def mk_resource(c, name, kind, available_days=31, available_from="09:00", available_to="17:00"):
    body = {"name": name, "kind": kind,
            "available_from": available_from, "available_to": available_to,
            "available_days": available_days}
    return c.post("/api/resources/", json=body).json()

def mk_task(c, proj_id, res_id, start, end, title="T"):
    return c.post("/api/tasks/", json={
        "title": title, "project_id": proj_id, "resource_ids": [res_id],
        "start_date": start, "end_date": end,
    }).json()


# ── /api/utilization ─────────────────────────────────────────────────────────

def test_overbooked_day_shows_negative_net(client):
    """Two 8h tasks on an 8h day → committed 16, capacity 8, net −8."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    for i in range(2):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01T09:00:00", "2026-06-01T17:00:00", f"T{i}")

    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-02"})
    assert resp.status_code == 200
    gpu_entry = next(r for r in resp.json() if r["resource_id"] == gpu["id"])
    by_day = {d["day"]: d for d in gpu_entry["days"]}

    assert by_day["2026-06-01"]["committed"] == pytest.approx(16.0)
    assert by_day["2026-06-01"]["capacity"] == pytest.approx(8.0)
    assert by_day["2026-06-01"]["net"] == pytest.approx(-8.0)
    assert by_day["2026-06-02"]["net"] == pytest.approx(8.0)  # nothing assigned → full spare


def test_at_capacity_shows_zero_net(client):
    """One 8h task on an 8h day → committed 8, capacity 8, net 0."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    mk_task(client, proj["id"], gpu["id"], "2026-06-01T09:00:00", "2026-06-01T17:00:00", "Solo")

    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-01"})
    gpu_entry = next(r for r in resp.json() if r["resource_id"] == gpu["id"])
    day = gpu_entry["days"][0]
    assert day["committed"] == pytest.approx(8.0)
    assert day["net"] == pytest.approx(0.0)


def test_idle_resource_shows_full_spare(client):
    mk_resource(client, "Idle", "human")  # Mon-Fri 8h
    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-01"})  # Mon
    idle = next(r for r in resp.json() if r["resource_name"] == "Idle")
    assert idle["days"][0]["committed"] == pytest.approx(0.0)
    assert idle["days"][0]["net"] == pytest.approx(8.0)


def test_utilization_includes_all_resources(client):
    mk_resource(client, "A", "human")
    mk_resource(client, "B", "gpu", available_days=127)
    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-01"})
    names = {r["resource_name"] for r in resp.json()}
    assert {"A", "B"} <= names


def test_utilization_day_count_matches_range(client):
    mk_resource(client, "R", "cpu", available_days=127)
    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-14"})
    r = next(e for e in resp.json() if e["resource_name"] == "R")
    assert len(r["days"]) == 14


# ── /api/resources/{id}/conflicts ────────────────────────────────────────────

def test_conflict_endpoint_flags_overbooked_days(client):
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    for i in range(2):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01T09:00:00", "2026-06-01T17:00:00", f"T{i}")

    resp = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-03"},
    )
    assert resp.status_code == 200
    flagged = {c["day"] for c in resp.json()}
    assert "2026-06-01" in flagged
    assert "2026-06-02" not in flagged
    assert "2026-06-03" not in flagged


def test_conflict_shows_contributing_tasks(client):
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    for title in ("Alpha", "Beta", "Gamma"):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01T09:00:00", "2026-06-01T17:00:00", title)

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert len(conflicts) == 1
    titles = {t["title"] for t in conflicts[0]["tasks"]}
    assert titles == {"Alpha", "Beta", "Gamma"}


def test_at_capacity_not_flagged_as_conflict(client):
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    mk_task(client, proj["id"], gpu["id"], "2026-06-01T09:00:00", "2026-06-01T17:00:00", "Solo")

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert conflicts == []


def test_conflict_overage_value(client):
    """Three 8h tasks on an 8h day → overage = 24 − 8 = 16h."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    for i in range(3):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01T09:00:00", "2026-06-01T17:00:00", f"T{i}")

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert conflicts[0]["overage"] == pytest.approx(16.0)
