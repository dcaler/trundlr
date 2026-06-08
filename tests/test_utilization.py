"""Integration tests for the utilization dashboard.

All resources use a 1-task-at-a-time model: capacity=1 per available day.
Committed = count of concurrent tasks. Conflict when committed > 1.
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

def mk_resource(c, name, kind, available_days=31):
    body = {"name": name, "kind": kind,
            "available_from": "00:00", "available_to": "23:59",
            "available_days": available_days}
    return c.post("/api/resources/", json=body).json()

def mk_task(c, proj_id, res_id, start, end, title="T"):
    return c.post("/api/tasks/", json={
        "title": title, "project_id": proj_id, "resource_ids": [res_id],
        "start_date": start, "end_date": end,
    }).json()


# ── /api/utilization ─────────────────────────────────────────────────────────

def test_overbooked_days_show_over_100_pct(client):
    """3 tasks on a resource → committed=3, capacity=1 → 300%."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    for i in range(3):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-02", f"T{i}")

    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-03"})
    assert resp.status_code == 200
    gpu_entry = next(r for r in resp.json() if r["resource_id"] == gpu["id"])
    by_day = {d["day"]: d for d in gpu_entry["days"]}

    assert by_day["2026-06-01"]["utilization"] == pytest.approx(300.0)
    assert by_day["2026-06-02"]["utilization"] == pytest.approx(300.0)
    assert by_day["2026-06-03"]["utilization"] == pytest.approx(0.0)


def test_fully_booked_shows_exactly_100_pct(client):
    """1 task on resource → committed=1, capacity=1 → 100% (not a conflict)."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", "Solo")

    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-01"})
    gpu_entry = next(r for r in resp.json() if r["resource_id"] == gpu["id"])
    assert gpu_entry["days"][0]["utilization"] == pytest.approx(100.0)
    assert gpu_entry["days"][0]["committed"] == pytest.approx(1.0)


def test_empty_resource_shows_zero_utilization(client):
    mk_resource(client, "Idle", "human")
    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-03"})
    idle = next(r for r in resp.json() if r["resource_name"] == "Idle")
    assert all(d["utilization"] == pytest.approx(0.0) for d in idle["days"])


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
    """3 tasks on resource → committed=3 > capacity=1 → conflict days flagged."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    for i in range(3):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-02", f"T{i}")

    resp = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-03"},
    )
    assert resp.status_code == 200
    conflicts = resp.json()
    flagged = {c["day"] for c in conflicts}

    assert "2026-06-01" in flagged
    assert "2026-06-02" in flagged
    assert "2026-06-03" not in flagged


def test_conflict_shows_contributing_tasks(client):
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", "Alpha")
    mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", "Beta")
    mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", "Gamma")

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert len(conflicts) == 1
    titles = {t["title"] for t in conflicts[0]["tasks"]}
    assert titles == {"Alpha", "Beta", "Gamma"}


def test_fully_booked_not_flagged_as_conflict(client):
    """1 task = 100% = NOT a conflict."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", "Solo")

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert conflicts == []


def test_conflict_overage_value(client):
    """3 tasks on resource → overage = 3 − 1 = 2."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", available_days=127)
    for i in range(3):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", f"T{i}")

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert conflicts[0]["overage"] == pytest.approx(2.0)


def test_two_concurrent_tasks_flagged(client):
    """2 tasks on same resource/day → conflict."""
    proj = mk_project(client)
    alice = mk_resource(client, "Alice", "human")
    mk_task(client, proj["id"], alice["id"], "2026-06-02", "2026-06-02", "A")
    mk_task(client, proj["id"], alice["id"], "2026-06-02", "2026-06-02", "B")

    resp = client.get("/api/utilization", params={"from": "2026-06-02", "to": "2026-06-02"})
    alice_entry = next(r for r in resp.json() if r["resource_id"] == alice["id"])
    day = alice_entry["days"][0]
    assert day["committed"] == pytest.approx(2.0)
    assert day["utilization"] == pytest.approx(200.0)

    conflicts = client.get(
        f"/api/resources/{alice['id']}/conflicts",
        params={"from": "2026-06-02", "to": "2026-06-02"},
    ).json()
    assert len(conflicts) == 1
    assert conflicts[0]["day"] == "2026-06-02"
