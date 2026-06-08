"""Tests for schedule API endpoints (Step 3.3).

Covers:
- GET /api/resources/{id}/schedule?from=&to=
- GET /api/resources/{id}/conflicts?from=&to=
- GET /api/utilization?from=&to=

For each: correct JSON shape, values matching engine output, 404 on missing
resource, and 422 on an inverted date range.
"""

from datetime import date

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
    """Client pre-populated with one resource and overlapping tasks."""
    test_client, engine = client
    with Session(engine) as session:
        project = Project(name="P1")
        session.add(project)
        session.flush()

        resource = Resource(name="GPU Node", kind=ResourceKind.gpu, capacity=4.0)
        other = Resource(name="CPU Node", kind=ResourceKind.cpu, capacity=8.0)
        session.add_all([resource, other])
        session.flush()

        # Two tasks: each load 2.0 on Jun 1–2 (total 4.0 = exactly full)
        t1 = Task(
            title="Task A",
            project_id=project.id,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 2),
            load=2.0,
        )
        # Third task: load 2.0 on Jun 2–3 → Jun 2 becomes 6.0 > 4.0 (conflict)
        t2 = Task(
            title="Task B",
            project_id=project.id,
            start_date=date(2026, 6, 2),
            end_date=date(2026, 6, 3),
            load=2.0,
        )
        t3 = Task(
            title="Task C",
            project_id=project.id,
            start_date=date(2026, 6, 2),
            end_date=date(2026, 6, 3),
            load=2.0,
        )
        session.add_all([t1, t2, t3])
        session.flush()
        session.add_all([
            TaskResource(task_id=t1.id, resource_id=resource.id),
            TaskResource(task_id=t2.id, resource_id=resource.id),
            TaskResource(task_id=t3.id, resource_id=resource.id),
        ])
        session.commit()

        # Return IDs for use in tests
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
        assert set(day.keys()) == {"day", "committed", "capacity", "utilization"}

    def test_utilization_values_match_engine(self, seeded_client):
        """Jun 1: tasks A only → 2.0 committed / 4.0 cap = 50%."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        day = resp.json()[0]
        assert day["day"] == "2026-06-01"
        assert day["committed"] == pytest.approx(2.0)
        assert day["capacity"] == pytest.approx(4.0)
        assert day["utilization"] == pytest.approx(50.0)

    def test_over_allocated_day_above_100(self, seeded_client):
        """Jun 2: tasks A+B+C → 6.0 / 4.0 = 150%."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-02", "to": "2026-06-02"},
        )
        day = resp.json()[0]
        assert day["utilization"] == pytest.approx(150.0)

    def test_zero_task_day(self, seeded_client):
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/schedule",
            params={"from": "2026-06-10", "to": "2026-06-10"},
        )
        day = resp.json()[0]
        assert day["committed"] == pytest.approx(0.0)
        assert day["utilization"] == pytest.approx(0.0)

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
        """Jun 1: 2.0/4.0 (under); Jun 2: 6.0/4.0 (over); Jun 3: 4.0/4.0 (full, not over)."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-03"},
        )
        assert resp.status_code == 200
        conflicts = resp.json()
        conflict_days = {c["day"] for c in conflicts}
        assert "2026-06-01" not in conflict_days  # under capacity → not a conflict
        assert "2026-06-02" in conflict_days       # 6.0 > 4.0 → conflict
        assert "2026-06-03" not in conflict_days   # exactly full (4.0 == 4.0) → not a conflict

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
        """Jun 2: 6.0 committed − 4.0 capacity = 2.0 overage."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/conflicts",
            params={"from": "2026-06-02", "to": "2026-06-02"},
        )
        c = resp.json()[0]
        assert c["overage"] == pytest.approx(2.0)

    def test_conflict_contributing_tasks(self, seeded_client):
        """Jun 2: all three tasks (A, B, C) are active."""
        test_client, resource_id, _ = seeded_client
        resp = test_client.get(
            f"/api/resources/{resource_id}/conflicts",
            params={"from": "2026-06-02", "to": "2026-06-02"},
        )
        titles = {t["title"] for t in resp.json()[0]["tasks"]}
        assert titles == {"Task A", "Task B", "Task C"}

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
        assert set(entry["days"][0].keys()) == {"day", "committed", "capacity", "utilization"}

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
