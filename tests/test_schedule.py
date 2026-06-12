"""Tests for schedule API endpoints (Step 3.3).

Covers:
- GET /api/resources/{id}/schedule?from=&to=
- GET /api/resources/{id}/conflicts?from=&to=
- GET /api/utilization?from=&to=

For each: correct JSON shape, values matching engine output, 404 on missing
resource, and 422 on an inverted date range.
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app
from app.models import Project, Resource, ResourceKind, Task, TaskResource, TaskStatus


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), engine
    app.dependency_overrides.clear()


@pytest.fixture
def seeded_client(client):
    """Client pre-populated with one resource and overlapping tasks.

    Resource (Mon-Fri, 09:00-17:00 = 8h/day) has:
      Task A: Jun 1 (Mon) 09-17 = 8h  → at capacity (net 0), no conflict
      Tasks B, C, D: Jun 2 (Tue) 09-17 = 8h each → 24h vs 8h → over, conflict
      Jun 3 (Wed): no tasks → 8h spare
    other resource has no tasks assigned (always full spare / no conflicts).
    """
    test_client, engine = client
    with Session(engine) as session:
        project = Project(name="P1")
        session.add(project)
        session.flush()

        resource = Resource(name="GPU Node", kind=ResourceKind.gpu,
                            available_from="09:00", available_to="17:00", available_days=31)
        other = Resource(name="CPU Node", kind=ResourceKind.cpu,
                         available_from="09:00", available_to="17:00", available_days=31)
        session.add_all([resource, other])
        session.flush()

        # Jun 1: one 8h task → at capacity. Jun 2: three 8h tasks → over capacity.
        t1 = Task(title="Task A", project_id=project.id,
                  start_date=datetime(2026, 6, 1, 9), end_date=datetime(2026, 6, 1, 17))
        t2 = Task(title="Task B", project_id=project.id,
                  start_date=datetime(2026, 6, 2, 9), end_date=datetime(2026, 6, 2, 17))
        t3 = Task(title="Task C", project_id=project.id,
                  start_date=datetime(2026, 6, 2, 9), end_date=datetime(2026, 6, 2, 17))
        t4 = Task(title="Task D", project_id=project.id,
                  start_date=datetime(2026, 6, 2, 9), end_date=datetime(2026, 6, 2, 17))
        session.add_all([t1, t2, t3, t4])
        session.flush()
        session.add_all([
            TaskResource(task_id=t.id, resource_id=resource.id)
            for t in (t1, t2, t3, t4)
        ])
        session.commit()

        session.refresh(resource)
        session.refresh(other)
        return test_client, resource.id, other.id

    return test_client


# ── /api/resources/{id}/schedule ─────────────────────────────────────────────

class TestResourceSchedule:
    def test_returns_per_day_list(self, seeded_client):
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-01", "to": "2026-06-03"},
        )
        assert resp.status_code == 200
        days = resp.json()
        assert len(days) == 3  # Jun 1, 2, 3

    def test_response_shape(self, seeded_client):
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        day = resp.json()[0]
        assert set(day.keys()) == {"day", "committed", "capacity", "net"}

    def test_values_match_engine(self, seeded_client):
        """Jun 1: Task A only → committed=8h, capacity=8h, net=0."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        day = resp.json()[0]
        assert day["day"] == "2026-06-01"
        assert day["committed"] == pytest.approx(8.0)
        assert day["capacity"] == pytest.approx(8.0)
        assert day["net"] == pytest.approx(0.0)

    def test_over_allocated_day_negative_net(self, seeded_client):
        """Jun 2: B+C+D → committed=24h, capacity=8h, net=−16."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-02", "to": "2026-06-02"},
        )
        day = resp.json()[0]
        assert day["committed"] == pytest.approx(24.0)
        assert day["net"] == pytest.approx(-16.0)

    def test_zero_task_day(self, seeded_client):
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-10", "to": "2026-06-10"},  # Wed, no tasks
        )
        day = resp.json()[0]
        assert day["committed"] == pytest.approx(0.0)
        assert day["net"] == pytest.approx(8.0)

    def test_404_on_missing_resource(self, client):
        test_client, _ = client
        resp = test_client.get(
            "/api/resources/9999/schedule",
            params={"from": "2026-06-01", "to": "2026-06-03"},
        )
        assert resp.status_code == 404

    def test_422_on_inverted_range(self, seeded_client):
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-05", "to": "2026-06-01"},
        )
        assert resp.status_code == 422

    def test_422_on_missing_date_params(self, seeded_client):
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(f"/api/resources/{resource_id}/schedule")
        assert resp.status_code == 422


# ── /api/resources/{id}/conflicts ────────────────────────────────────────────

class TestResourceConflicts:
    def test_returns_only_over_allocated_days(self, seeded_client):
        """Jun 1: A only (100%, no conflict); Jun 2: A+B+C (300%, conflict); Jun 3: 0 tasks (no conflict)."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-03"},
        )
        assert resp.status_code == 200
        conflicts = resp.json()
        conflict_days = {c["day"] for c in conflicts}
        assert "2026-06-01" not in conflict_days  # 8h on 8h day = at capacity, not a conflict
        assert "2026-06-02" in conflict_days       # 24h > capacity 8h → conflict
        assert "2026-06-03" not in conflict_days   # 0 tasks → no conflict

    def test_conflict_shape(self, seeded_client):
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/conflicts",
            params={"from": "2026-06-02", "to": "2026-06-02"},
        )
        c = resp.json()[0]
        assert set(c.keys()) == {"day", "committed", "capacity", "overage", "tasks"}
        assert isinstance(c["tasks"], list)
        assert set(c["tasks"][0].keys()) == {"id", "title"}

    def test_conflict_overage_value(self, seeded_client):
        """Jun 2: 24h committed − 8h capacity = overage 16h."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/conflicts",
            params={"from": "2026-06-02", "to": "2026-06-02"},
        )
        c = resp.json()[0]
        assert c["overage"] == pytest.approx(16.0)

    def test_conflict_contributing_tasks(self, seeded_client):
        """Jun 2: the three Jun-2 tasks (B, C, D) are active."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/conflicts",
            params={"from": "2026-06-02", "to": "2026-06-02"},
        )
        titles = {t["title"] for t in resp.json()[0]["tasks"]}
        assert titles == {"Task B", "Task C", "Task D"}

    def test_no_conflict_when_within_capacity(self, seeded_client):
        test_client, _, other_id = seeded_client
        resp = test_client.get(
            f"/api/resources/{other_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-03"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_404_on_missing_resource(self, client):
        test_client, _ = client
        resp = test_client.get(
            "/api/resources/9999/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-03"},
        )
        assert resp.status_code == 404

    def test_422_on_inverted_range(self, seeded_client):
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/conflicts",
            params={"from": "2026-06-05", "to": "2026-06-01"},
        )
        assert resp.status_code == 422


# ── /api/utilization ─────────────────────────────────────────────────────────

class TestUtilization:
    def test_returns_all_resources(self, seeded_client):
        test_client, _, _ = seeded_client
        resp = test_client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-03"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2  # GPU Node + CPU Node

    def test_response_shape(self, seeded_client):
        test_client, _, _ = seeded_client
        resp = test_client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        entry = resp.json()[0]
        assert set(entry.keys()) == {"resource_id", "resource_name", "days"}
        assert isinstance(entry["days"], list)
        assert set(entry["days"][0].keys()) == {"day", "committed", "capacity", "net"}

    def test_days_count_matches_range(self, seeded_client):
        test_client, _, _ = seeded_client
        resp = test_client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        for entry in resp.json():
            assert len(entry["days"]) == 7

    def test_422_on_inverted_range(self, client):
        test_client, _ = client
        resp = test_client.get(
            "/api/utilization",
            params={"from": "2026-06-05", "to": "2026-06-01"},
        )
        assert resp.status_code == 422

    def test_422_on_missing_date_params(self, client):
        test_client, _ = client
        resp = test_client.get("/api/utilization")
        assert resp.status_code == 422
