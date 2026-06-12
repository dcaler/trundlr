"""End-to-end test suite.

A single realistic scenario that exercises the complete application stack:

  create project → create resources → create tasks → assign to resources →
  query schedule (per-resource hours) → query conflicts (over-allocation)
  → query cross-resource utilization → lifecycle transitions → teardown

Hours-based model:
  - capacity = available hours per day (window length minus blockouts)
  - committed = assigned task-hours (interval overlap with the day)
  - net = capacity − committed (positive = spare, negative = over)
  - committed == capacity → at capacity (NOT a conflict)
  - committed >  capacity → conflict (over-allocated)

Resources here are 8h/day (09:00–17:00). Tasks are single-day 8h blocks; two
on the same day = 16h vs 8h = over-allocation.

To run against a live container:
    BASE_URL=http://localhost:8000 pytest tests/test_e2e.py -v

Without the env var, tests run against the TestClient / in-memory DB.
"""

import os

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app

# Single-day 8h task blocks (09:00–17:00) on a weekday and a clear day.
DAY1 = ("2026-06-01T09:00:00", "2026-06-01T17:00:00")  # Monday
FAR  = ("2026-06-10T09:00:00", "2026-06-10T17:00:00")  # Wednesday, no overlap


# ── Client fixture: TestClient (default) or live container ──────────────────

@pytest.fixture(scope="module")
def client():
    base_url = os.getenv("BASE_URL")
    if base_url:
        with httpx.Client(base_url=base_url) as c:
            yield c
        return

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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Scenario state: built up incrementally across the test class ─────────────

class TestFullScenario:
    """Single project, two 8h/day resources (Alice + GPU), stacked tasks.

    Jun 1 (Mon): Data collection (Alice) + Model training A (GPU) — each sole,
    at capacity, no conflict. Later stages stack more 8h tasks on Jun 1 to
    create and then resolve over-allocation.
    """

    # ── Stage 1: project and resources ──────────────────────────────────────

    def test_01_health_check(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_02_create_project(self, client):
        resp = client.post("/api/projects/", json={
            "name": "ML Pipeline Q3",
            "description": "End-to-end ML project for Q3",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "ML Pipeline Q3"
        assert "id" in data
        TestFullScenario._project_id = data["id"]

    def test_03_project_appears_in_list(self, client):
        resp = client.get("/api/projects/")
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()]
        assert TestFullScenario._project_id in ids

    def test_04_create_human_resource(self, client):
        resp = client.post("/api/resources/", json={
            "name": "Alice",
            "kind": "human",
            "available_from": "09:00",
            "available_to": "17:00",
            "available_days": 31,  # Mon–Fri
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["kind"] == "human"
        assert data["available_from"] == "09:00"
        assert data["available_to"] == "17:00"
        TestFullScenario._alice_id = data["id"]

    def test_05_create_gpu_resource(self, client):
        resp = client.post("/api/resources/", json={
            "name": "GPU Node",
            "kind": "gpu",
            "available_from": "09:00",
            "available_to": "17:00",
            "available_days": 127,  # all 7 days, 8h each
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["kind"] == "gpu"
        TestFullScenario._gpu_id = data["id"]

    def test_06_both_resources_in_list(self, client):
        resp = client.get("/api/resources/")
        assert resp.status_code == 200
        names = {r["name"] for r in resp.json()}
        assert {"Alice", "GPU Node"} <= names

    # ── Stage 2: initial non-conflicting tasks ───────────────────────────────

    def test_07_create_task_data_collection(self, client):
        resp = client.post("/api/tasks/", json={
            "title": "Data collection",
            "project_id": TestFullScenario._project_id,
            "resource_ids": [TestFullScenario._alice_id],
            "start_date": DAY1[0], "end_date": DAY1[1],
            "status": "in_progress",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert TestFullScenario._alice_id in data["resource_ids"]
        TestFullScenario._task_data_id = data["id"]

    def test_08_create_task_training_a(self, client):
        resp = client.post("/api/tasks/", json={
            "title": "Model training A",
            "project_id": TestFullScenario._project_id,
            "resource_ids": [TestFullScenario._gpu_id],
            "start_date": DAY1[0], "end_date": DAY1[1],
        })
        assert resp.status_code == 201
        TestFullScenario._task_train_a_id = resp.json()["id"]

    def test_09_all_tasks_under_project(self, client):
        resp = client.get(f"/api/tasks/?project_id={TestFullScenario._project_id}")
        assert resp.status_code == 200
        titles = {t["title"] for t in resp.json()}
        assert {"Data collection", "Model training A"} <= titles

    # ── Stage 3: clean utilization — 1 task per resource, at capacity ────────

    def test_10_alice_schedule_at_capacity(self, client):
        """Jun 1: 8h task on 8h day → committed=8, capacity=8, net=0; Jun 2 spare."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/schedule",
            params={"from": "2026-06-01", "to": "2026-06-02"},
        )
        assert resp.status_code == 200
        days = {d["day"]: d for d in resp.json()}
        assert days["2026-06-01"]["committed"] == pytest.approx(8.0)
        assert days["2026-06-01"]["capacity"] == pytest.approx(8.0)
        assert days["2026-06-01"]["net"] == pytest.approx(0.0)
        assert days["2026-06-02"]["committed"] == pytest.approx(0.0)
        assert days["2026-06-02"]["net"] == pytest.approx(8.0)

    def test_11_alice_no_conflicts_with_one_task(self, client):
        """At capacity is NOT a conflict."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_12_gpu_schedule_at_capacity(self, client):
        """Jun 1: training A alone → net=0; Jun 6-7: no tasks → full spare."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/schedule",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 200
        days = {d["day"]: d for d in resp.json()}
        assert days["2026-06-01"]["committed"] == pytest.approx(8.0)
        assert days["2026-06-01"]["net"] == pytest.approx(0.0)
        assert days["2026-06-06"]["committed"] == pytest.approx(0.0)
        assert days["2026-06-07"]["committed"] == pytest.approx(0.0)

    def test_13_gpu_no_conflicts_with_one_task(self, client):
        resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    # ── Stage 4: cross-resource utilization endpoint ─────────────────────────

    def test_14_utilization_covers_all_resources(self, client):
        resp = client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 200
        entries = resp.json()
        resource_ids = {e["resource_id"] for e in entries}
        assert TestFullScenario._alice_id in resource_ids
        assert TestFullScenario._gpu_id in resource_ids

    def test_15_utilization_day_count_matches_range(self, client):
        resp = client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        for entry in resp.json():
            assert len(entry["days"]) == 7

    def test_16_utilization_response_shape(self, client):
        resp = client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        entry = resp.json()[0]
        assert set(entry.keys()) == {"resource_id", "resource_name", "days"}
        day = entry["days"][0]
        assert set(day.keys()) == {"day", "committed", "capacity", "net"}

    # ── Stage 5: add tasks that create conflicts ──────────────────────────────

    def test_17_add_analysis_causes_alice_conflict(self, client):
        """Analysis stacks on Jun 1 with Data collection → 16h vs 8h = conflict."""
        resp = client.post("/api/tasks/", json={
            "title": "Analysis",
            "project_id": TestFullScenario._project_id,
            "resource_ids": [TestFullScenario._alice_id],
            "start_date": DAY1[0], "end_date": DAY1[1],
        })
        assert resp.status_code == 201
        TestFullScenario._task_analysis_id = resp.json()["id"]

    def test_18_alice_conflict_on_overlap_day(self, client):
        """Jun 1: 2 tasks → over; Jun 2: nothing → clean."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-05"},
        )
        assert resp.status_code == 200
        conflict_days = {c["day"] for c in resp.json()}
        assert "2026-06-01" in conflict_days
        assert "2026-06-02" not in conflict_days

    def test_19_alice_conflict_details_jun_01(self, client):
        """Jun 1: committed=16h, capacity=8h, overage=8h; both tasks listed."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        conflicts = resp.json()
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["day"] == "2026-06-01"
        assert c["committed"] == pytest.approx(16.0)
        assert c["capacity"] == pytest.approx(8.0)
        assert c["overage"] == pytest.approx(8.0)
        titles = {t["title"] for t in c["tasks"]}
        assert "Data collection" in titles
        assert "Analysis" in titles

    def test_20_add_training_b_causes_gpu_conflict(self, client):
        """Training B stacks on Jun 1 with Training A → GPU conflict."""
        resp = client.post("/api/tasks/", json={
            "title": "Model training B",
            "project_id": TestFullScenario._project_id,
            "resource_ids": [TestFullScenario._gpu_id],
            "start_date": DAY1[0], "end_date": DAY1[1],
        })
        assert resp.status_code == 201
        TestFullScenario._task_train_b_id = resp.json()["id"]

    def test_21_gpu_conflict_on_overlap_day(self, client):
        resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        conflict_days = {c["day"] for c in resp.json()}
        assert "2026-06-01" in conflict_days
        assert "2026-06-02" not in conflict_days

    # ── Stage 6: deeper conflicts (3 tasks each) ─────────────────────────────

    def test_22_add_emergency_fix_deepens_alice_conflict(self, client):
        """3rd 8h task for Alice on Jun 1 → committed 24h, overage 16h."""
        resp = client.post("/api/tasks/", json={
            "title": "Emergency fix",
            "project_id": TestFullScenario._project_id,
            "resource_ids": [TestFullScenario._alice_id],
            "start_date": DAY1[0], "end_date": DAY1[1],
        })
        assert resp.status_code == 201
        TestFullScenario._task_emerg_id = resp.json()["id"]

    def test_23_alice_conflict_overage_on_jun_01(self, client):
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        conflicts = resp.json()
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["committed"] == pytest.approx(24.0)
        assert c["capacity"] == pytest.approx(8.0)
        assert c["overage"] == pytest.approx(16.0)
        titles = {t["title"] for t in c["tasks"]}
        assert {"Data collection", "Analysis", "Emergency fix"} <= titles

    def test_24_add_model_eval_deepens_gpu_conflict(self, client):
        """3rd task for GPU on Jun 1."""
        resp = client.post("/api/tasks/", json={
            "title": "Model eval",
            "project_id": TestFullScenario._project_id,
            "resource_ids": [TestFullScenario._gpu_id],
            "start_date": DAY1[0], "end_date": DAY1[1],
        })
        assert resp.status_code == 201
        TestFullScenario._task_eval_id = resp.json()["id"]

    def test_25_gpu_conflict_lists_three_contributing_tasks(self, client):
        """Jun 1: training A + training B + model eval all active."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        titles = {t["title"] for t in resp.json()[0]["tasks"]}
        assert {"Model training A", "Model training B", "Model eval"} <= titles

    # ── Stage 7: task lifecycle transitions ──────────────────────────────────

    def test_26_status_transitions(self, client):
        task_id = TestFullScenario._task_data_id
        for status in ("in_progress", "blocked", "done"):
            resp = client.patch(f"/api/tasks/{task_id}", json={"status": status})
            assert resp.status_code == 200
            assert resp.json()["status"] == status

    def test_27_patch_task_dates_updates_schedule(self, client):
        """Move emergency fix to a clear day → Alice Jun 1 back to 2 tasks (16h)."""
        task_id = TestFullScenario._task_emerg_id
        resp = client.patch(f"/api/tasks/{task_id}", json={
            "start_date": FAR[0], "end_date": FAR[1],
        })
        assert resp.status_code == 200

        conflicts_resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        c = conflicts_resp.json()[0]
        assert c["committed"] == pytest.approx(16.0)  # Data collection + Analysis
        assert c["overage"] == pytest.approx(8.0)

    def test_28_unassign_resource_removes_from_schedule(self, client):
        """Unassign model eval from GPU → GPU Jun 1 back to 2 tasks (16h)."""
        resp = client.patch(
            f"/api/tasks/{TestFullScenario._task_eval_id}",
            json={"resource_ids": []},
        )
        assert resp.status_code == 200
        assert resp.json()["resource_ids"] == []

        conflicts_resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        c = conflicts_resp.json()[0]
        assert c["committed"] == pytest.approx(16.0)
        assert c["overage"] == pytest.approx(8.0)

    # ── Stage 8: validation gates (schedule endpoints) ───────────────────────

    def test_29_schedule_rejects_inverted_range(self, client):
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/schedule",
            params={"from": "2026-06-10", "to": "2026-06-01"},
        )
        assert resp.status_code == 422

    def test_30_conflicts_rejects_inverted_range(self, client):
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-10", "to": "2026-06-01"},
        )
        assert resp.status_code == 422

    def test_31_utilization_rejects_inverted_range(self, client):
        resp = client.get(
            "/api/utilization",
            params={"from": "2026-06-10", "to": "2026-06-01"},
        )
        assert resp.status_code == 422

    def test_32_schedule_404_on_unknown_resource(self, client):
        resp = client.get(
            "/api/resources/99999/schedule",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 404

    # ── Stage 9: teardown — cascade delete cleans up ─────────────────────────

    def test_33_delete_project_cascades_all_tasks(self, client):
        task_ids = [
            TestFullScenario._task_data_id,
            TestFullScenario._task_train_a_id,
            TestFullScenario._task_analysis_id,
            TestFullScenario._task_train_b_id,
            TestFullScenario._task_emerg_id,
            TestFullScenario._task_eval_id,
        ]
        resp = client.delete(f"/api/projects/{TestFullScenario._project_id}")
        assert resp.status_code == 204

        for task_id in task_ids:
            assert client.get(f"/api/tasks/{task_id}").status_code == 404

    def test_34_resources_survive_project_deletion(self, client):
        assert client.get(f"/api/resources/{TestFullScenario._alice_id}").status_code == 200
        assert client.get(f"/api/resources/{TestFullScenario._gpu_id}").status_code == 200

    def test_35_delete_resources(self, client):
        assert client.delete(f"/api/resources/{TestFullScenario._alice_id}").status_code == 204
        assert client.delete(f"/api/resources/{TestFullScenario._gpu_id}").status_code == 204
        assert client.get(f"/api/resources/{TestFullScenario._alice_id}").status_code == 404
        assert client.get(f"/api/resources/{TestFullScenario._gpu_id}").status_code == 404
