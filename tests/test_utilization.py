"""Integration tests for the utilization dashboard (Step 4.4).

The plan specifies: "render against seeded overbooked resource; assert the
flagged day shows the warning state."  Without a browser, these tests hit the
API endpoints the JS dashboard consumes and assert:
  - over-allocated days appear with utilization > 100 in /api/utilization
  - those same days appear in /api/resources/{id}/conflicts with contributing tasks
  - fully-booked days (committed == capacity) are NOT flagged as conflicts
  - resources with no tasks show 0% utilization throughout
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

def mk_project(c): return c.post("/api/projects/", json={"name": "P"}).json()
def mk_resource(c, name, kind, cap=None):
    if kind == "human":
        body = {"name": name, "kind": kind,
                "available_from": "09:00", "available_to": "17:00", "available_days": 31}
    else:
        body = {"name": name, "kind": kind, "capacity": cap}
    return c.post("/api/resources/", json=body).json()
def mk_task(c, proj_id, res_id, start, end, load, title="T"):
    return c.post("/api/tasks/", json={
        "title": title, "project_id": proj_id, "resource_id": res_id,
        "start_date": start, "end_date": end, "load": load,
    }).json()


# ── /api/utilization ─────────────────────────────────────────────────────────

def test_overbooked_days_show_over_100_pct(client):
    """3 tasks × load 2 on a 4-slot GPU → 150% for those days."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", 4.0)
    for i in range(3):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-02", 2.0, f"T{i}")

    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-03"})
    assert resp.status_code == 200
    gpu_entry = next(r for r in resp.json() if r["resource_id"] == gpu["id"])
    by_day = {d["day"]: d for d in gpu_entry["days"]}

    assert by_day["2026-06-01"]["utilization"] == pytest.approx(150.0)
    assert by_day["2026-06-02"]["utilization"] == pytest.approx(150.0)
    assert by_day["2026-06-03"]["utilization"] == pytest.approx(0.0)


def test_fully_booked_shows_exactly_100_pct(client):
    """2 tasks × load 2 on a 4-slot GPU → exactly 100% (not a conflict)."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", 4.0)
    for i in range(2):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", 2.0, f"T{i}")

    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-01"})
    gpu_entry = next(r for r in resp.json() if r["resource_id"] == gpu["id"])
    assert gpu_entry["days"][0]["utilization"] == pytest.approx(100.0)
    assert gpu_entry["days"][0]["committed"] == pytest.approx(4.0)


def test_empty_resource_shows_zero_utilization(client):
    mk_resource(client, "Idle", "human")
    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-03"})
    idle = next(r for r in resp.json() if r["resource_name"] == "Idle")
    # Jun 1-2 are Mon-Tue (capacity 8h); Jun 3 is Wed — all have 0 committed load.
    assert all(d["utilization"] == pytest.approx(0.0) for d in idle["days"])


def test_utilization_includes_all_resources(client):
    mk_resource(client, "A", "human")
    mk_resource(client, "B", "gpu", 4.0)
    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-01"})
    names = {r["resource_name"] for r in resp.json()}
    assert {"A", "B"} <= names


def test_utilization_day_count_matches_range(client):
    mk_resource(client, "R", "cpu", 2.0)
    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-14"})
    r = next(e for e in resp.json() if e["resource_name"] == "R")
    assert len(r["days"]) == 14


# ── /api/resources/{id}/conflicts ────────────────────────────────────────────

def test_conflict_endpoint_flags_overbooked_days(client):
    """The dashboard calls the conflicts endpoint to get task-level detail."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", 4.0)
    for i in range(3):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-02", 2.0, f"T{i}")

    resp = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-03"},
    )
    assert resp.status_code == 200
    conflicts = resp.json()
    flagged = {c["day"] for c in conflicts}

    assert "2026-06-01" in flagged   # over-allocated → warning state
    assert "2026-06-02" in flagged
    assert "2026-06-03" not in flagged  # no load → not flagged


def test_conflict_shows_contributing_tasks(client):
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", 4.0)
    mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", 2.0, "Alpha")
    mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", 2.0, "Beta")
    mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", 2.0, "Gamma")

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert len(conflicts) == 1
    titles = {t["title"] for t in conflicts[0]["tasks"]}
    assert titles == {"Alpha", "Beta", "Gamma"}


def test_fully_booked_not_flagged_as_conflict(client):
    """The off-by-one guard: committed == capacity is NOT a conflict."""
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", 4.0)
    for i in range(2):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", 2.0, f"T{i}")

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert conflicts == []


def test_conflict_overage_value(client):
    proj = mk_project(client)
    gpu  = mk_resource(client, "GPU", "gpu", 4.0)
    for i in range(3):
        mk_task(client, proj["id"], gpu["id"], "2026-06-01", "2026-06-01", 2.0, f"T{i}")

    conflicts = client.get(
        f"/api/resources/{gpu['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert conflicts[0]["overage"] == pytest.approx(2.0)  # 6.0 − 4.0


def test_human_hours_over_allocation_flagged(client):
    """Humans (hours/day) use the same detection path as GPU slots."""
    proj = mk_project(client)
    alice = mk_resource(client, "Alice", "human")
    mk_task(client, proj["id"], alice["id"], "2026-06-01", "2026-06-01", 5.0, "A")
    mk_task(client, proj["id"], alice["id"], "2026-06-01", "2026-06-01", 5.0, "B")

    resp = client.get("/api/utilization", params={"from": "2026-06-01", "to": "2026-06-01"})
    alice_entry = next(r for r in resp.json() if r["resource_id"] == alice["id"])
    day = alice_entry["days"][0]
    assert day["committed"] == pytest.approx(10.0)
    assert day["utilization"] == pytest.approx(125.0)   # 10/8 * 100

    conflicts = client.get(
        f"/api/resources/{alice['id']}/conflicts",
        params={"from": "2026-06-01", "to": "2026-06-01"},
    ).json()
    assert len(conflicts) == 1
    assert conflicts[0]["day"] == "2026-06-01"
