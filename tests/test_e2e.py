"""End-to-end test suite (Step 6.1).

A single realistic scenario that exercises the complete application stack:

  create project → create resources → create tasks → assign to resources →
  query schedule (per-resource utilization) → query conflicts (over-allocation)
  → query cross-resource utilization → lifecycle transitions → teardown

This file is designed to run against the built Docker container in CI
(via httpx pointing at a live URL), and also against the in-process
TestClient (the default here, for fast local runs and pre-merge gates).

To run against a live container:
    BASE_URL=http://localhost:8000 pytest tests/test_e2e.py -v

Without the env var, tests run against the TestClient / in-memory DB.
"""

import os
from datetime import date, timedelta

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app


# ── Client fixture: TestClient (default) or live container ──────────────────

@pytest.fixture(scope="module")
def client():
    base_url = os.getenv("BASE_URL")
    if base_url:
        # CI mode: point at the running Docker container
        with httpx.Client(base_url=base_url) as c:
            yield c
        return

    # Local mode: in-process TestClient with an in-memory SQLite DB
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
    """
    Single project, two resources (human + GPU), multiple tasks.

    Timeline used (all Alice tasks on Mon–Fri; availability 09:00–17:00):
      Jun 01–05: Alice  4h/day "Data collection"     → 50% utilization
      Jun 03–05: Alice  4h/day "Analysis"             → 100% (with Data collection)
      Jun 04–05: Alice  2h/day "Emergency fix" (added later → conflict Jun 4-5)
      Jun 01–05: GPU    2 slots "Model training A"    → 50%
      Jun 03–07: GPU    2 slots "Model training B"    → 50% (Jun 3–5: full)
      Jun 03–05: GPU    1 slot  "Model eval" (later)  → conflict Jun 3–5
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
            "capacity": 4.0,
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

    # ── Stage 2: tasks created and assigned ─────────────────────────────────

    def test_07_create_task_data_collection(self, client):
        resp = client.post("/api/tasks/", json={
            "title": "Data collection",
            "project_id": TestFullScenario._project_id,
            "resource_id": TestFullScenario._alice_id,
            "start_date": "2026-06-01",
            "end_date": "2026-06-05",
            "load": 4.0,
            "status": "in_progress",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["resource_id"] == TestFullScenario._alice_id
        assert data["load"] == pytest.approx(4.0)
        TestFullScenario._task_data_id = data["id"]

    def test_08_create_task_analysis(self, client):
        resp = client.post("/api/tasks/", json={
            "title": "Analysis",
            "project_id": TestFullScenario._project_id,
            "resource_id": TestFullScenario._alice_id,
            "start_date": "2026-06-03",
            "end_date": "2026-06-05",  # Wed–Fri; stays on workdays
            "load": 4.0,
        })
        assert resp.status_code == 201
        TestFullScenario._task_analysis_id = resp.json()["id"]

    def test_09_create_task_training_a(self, client):
        resp = client.post("/api/tasks/", json={
            "title": "Model training A",
            "project_id": TestFullScenario._project_id,
            "resource_id": TestFullScenario._gpu_id,
            "start_date": "2026-06-01",
            "end_date": "2026-06-05",
            "load": 2.0,
        })
        assert resp.status_code == 201
        TestFullScenario._task_train_a_id = resp.json()["id"]

    def test_10_create_task_training_b(self, client):
        resp = client.post("/api/tasks/", json={
            "title": "Model training B",
            "project_id": TestFullScenario._project_id,
            "resource_id": TestFullScenario._gpu_id,
            "start_date": "2026-06-03",
            "end_date": "2026-06-07",
            "load": 2.0,
        })
        assert resp.status_code == 201
        TestFullScenario._task_train_b_id = resp.json()["id"]

    def test_11_all_tasks_under_project(self, client):
        resp = client.get(f"/api/tasks/?project_id={TestFullScenario._project_id}")
        assert resp.status_code == 200
        titles = {t["title"] for t in resp.json()}
        assert {"Data collection", "Analysis", "Model training A", "Model training B"} <= titles

    # ── Stage 3: schedule query — correct utilization before conflicts ────────

    def test_12_alice_schedule_shows_partial_utilization_before_overlap(self, client):
        """Jun 1-2: only Data collection (4h/8h = 50%)."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/schedule",
            params={"from": "2026-06-01", "to": "2026-06-02"},
        )
        assert resp.status_code == 200
        days = {d["day"]: d for d in resp.json()}
        assert days["2026-06-01"]["utilization"] == pytest.approx(50.0)
        assert days["2026-06-01"]["committed"] == pytest.approx(4.0)
        assert days["2026-06-01"]["capacity"] == pytest.approx(8.0)
        assert days["2026-06-02"]["utilization"] == pytest.approx(50.0)

    def test_13_alice_schedule_shows_full_utilization_on_overlap_days(self, client):
        """Jun 3-5: Data collection + Analysis = 4+4 = 8h = exactly 100%."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/schedule",
            params={"from": "2026-06-03", "to": "2026-06-05"},
        )
        assert resp.status_code == 200
        for day in resp.json():
            assert day["utilization"] == pytest.approx(100.0)
            assert day["committed"] == pytest.approx(8.0)

    def test_14_alice_no_conflicts_at_full_capacity(self, client):
        """100% utilization is NOT a conflict; over 100% is."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_15_gpu_schedule_partial_then_full(self, client):
        """
        Jun 1-2: training A only (2.0/4.0 = 50%).
        Jun 3-5: training A + B (4.0/4.0 = 100%, not a conflict).
        Jun 6-7: training B only (2.0/4.0 = 50%).
        """
        resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/schedule",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 200
        days = {d["day"]: d for d in resp.json()}
        assert days["2026-06-01"]["utilization"] == pytest.approx(50.0)
        assert days["2026-06-02"]["utilization"] == pytest.approx(50.0)
        assert days["2026-06-03"]["utilization"] == pytest.approx(100.0)
        assert days["2026-06-05"]["utilization"] == pytest.approx(100.0)
        assert days["2026-06-06"]["utilization"] == pytest.approx(50.0)
        assert days["2026-06-07"]["utilization"] == pytest.approx(50.0)

    def test_16_gpu_no_conflicts_before_eval_task(self, client):
        resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    # ── Stage 4: cross-resource utilization endpoint ─────────────────────────

    def test_17_utilization_covers_all_resources(self, client):
        resp = client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert resp.status_code == 200
        entries = resp.json()
        resource_ids = {e["resource_id"] for e in entries}
        assert TestFullScenario._alice_id in resource_ids
        assert TestFullScenario._gpu_id in resource_ids

    def test_18_utilization_day_count_matches_range(self, client):
        resp = client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        for entry in resp.json():
            assert len(entry["days"]) == 7

    def test_19_utilization_response_shape(self, client):
        resp = client.get(
            "/api/utilization",
            params={"from": "2026-06-01", "to": "2026-06-01"},
        )
        entry = resp.json()[0]
        assert set(entry.keys()) == {"resource_id", "resource_name", "days"}
        day = entry["days"][0]
        assert set(day.keys()) == {"day", "committed", "capacity", "utilization"}

    # ── Stage 5: add tasks that push resources over capacity ─────────────────

    def test_20_add_emergency_fix_causes_alice_conflict(self, client):
        """2h emergency on Jun 4-5 pushes Alice from 8h → 10h (> 8h capacity)."""
        resp = client.post("/api/tasks/", json={
            "title": "Emergency fix",
            "project_id": TestFullScenario._project_id,
            "resource_id": TestFullScenario._alice_id,
            "start_date": "2026-06-04",
            "end_date": "2026-06-05",
            "load": 2.0,
        })
        assert resp.status_code == 201
        TestFullScenario._task_emerg_id = resp.json()["id"]

    def test_21_alice_conflict_on_overloaded_days(self, client):
        """Jun 4-5 should now be flagged; Jun 1-3 should not."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-05"},
        )
        assert resp.status_code == 200
        conflicts = resp.json()
        conflict_days = {c["day"] for c in conflicts}
        assert "2026-06-03" not in conflict_days  # 8h exactly full → not over
        assert "2026-06-04" in conflict_days
        assert "2026-06-05" in conflict_days

    def test_22_conflict_overage_and_contributing_tasks(self, client):
        """Jun 4: committed=10.0, capacity=8.0, overage=2.0; both tasks listed."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-04", "to": "2026-06-04"},
        )
        conflicts = resp.json()
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["day"] == "2026-06-04"
        assert c["committed"] == pytest.approx(10.0)
        assert c["capacity"] == pytest.approx(8.0)
        assert c["overage"] == pytest.approx(2.0)
        titles = {t["title"] for t in c["tasks"]}
        assert "Analysis" in titles
        assert "Emergency fix" in titles

    def test_23_add_model_eval_causes_gpu_conflict(self, client):
        """1-slot eval on Jun 3-5 pushes GPU from 4.0 → 5.0 slots."""
        resp = client.post("/api/tasks/", json={
            "title": "Model eval",
            "project_id": TestFullScenario._project_id,
            "resource_id": TestFullScenario._gpu_id,
            "start_date": "2026-06-03",
            "end_date": "2026-06-05",
            "load": 1.0,
        })
        assert resp.status_code == 201
        TestFullScenario._task_eval_id = resp.json()["id"]

    def test_24_gpu_conflict_on_eval_overlap_days(self, client):
        resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        conflict_days = {c["day"] for c in resp.json()}
        assert "2026-06-01" not in conflict_days  # 2.0/4.0 = 50% → clean
        assert "2026-06-02" not in conflict_days
        assert "2026-06-03" in conflict_days      # 5.0 > 4.0 → conflict
        assert "2026-06-04" in conflict_days
        assert "2026-06-05" in conflict_days
        assert "2026-06-06" not in conflict_days  # 2.0/4.0 after eval ends
        assert "2026-06-07" not in conflict_days

    def test_25_gpu_conflict_lists_three_contributing_tasks(self, client):
        """Jun 3: training A + training B + model eval all active."""
        resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/conflicts",
            params={"from": "2026-06-03", "to": "2026-06-03"},
        )
        titles = {t["title"] for t in resp.json()[0]["tasks"]}
        assert {"Model training A", "Model training B", "Model eval"} <= titles

    # ── Stage 6: task lifecycle transitions ──────────────────────────────────

    def test_26_status_transitions(self, client):
        task_id = TestFullScenario._task_data_id
        for status in ("in_progress", "blocked", "done"):
            resp = client.patch(f"/api/tasks/{task_id}", json={"status": status})
            assert resp.status_code == 200
            assert resp.json()["status"] == status

    def test_27_patch_task_dates_updates_schedule(self, client):
        """Move emergency fix to Jun 10-11 (no overlap) → Alice conflict disappears."""
        task_id = TestFullScenario._task_emerg_id
        resp = client.patch(f"/api/tasks/{task_id}", json={
            "start_date": "2026-06-10",
            "end_date": "2026-06-11",
        })
        assert resp.status_code == 200

        conflicts_resp = client.get(
            f"/api/resources/{TestFullScenario._alice_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert conflicts_resp.json() == []

    def test_28_unassign_resource_removes_from_schedule(self, client):
        """Unassign model eval → GPU conflict resolves on Jun 3-5."""
        resp = client.patch(
            f"/api/tasks/{TestFullScenario._task_eval_id}",
            json={"resource_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["resource_id"] is None

        conflicts_resp = client.get(
            f"/api/resources/{TestFullScenario._gpu_id}/conflicts",
            params={"from": "2026-06-01", "to": "2026-06-07"},
        )
        assert conflicts_resp.json() == []

    # ── Stage 7: validation gates (schedule endpoints) ───────────────────────

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

    # ── Stage 8: teardown — cascade delete cleans up ─────────────────────────

    def test_33_delete_project_cascades_all_tasks(self, client):
        task_ids = [
            TestFullScenario._task_data_id,
            TestFullScenario._task_analysis_id,
            TestFullScenario._task_train_a_id,
            TestFullScenario._task_train_b_id,
            TestFullScenario._task_emerg_id,
            TestFullScenario._task_eval_id,
        ]
        resp = client.delete(f"/api/projects/{TestFullScenario._project_id}")
        assert resp.status_code == 204

        for task_id in task_ids:
            assert client.get(f"/api/tasks/{task_id}").status_code == 404

    def test_34_resources_survive_project_deletion(self, client):
        """Resources are independent; deleting the project must not delete them."""
        assert client.get(f"/api/resources/{TestFullScenario._alice_id}").status_code == 200
        assert client.get(f"/api/resources/{TestFullScenario._gpu_id}").status_code == 200

    def test_35_delete_resources(self, client):
        assert client.delete(f"/api/resources/{TestFullScenario._alice_id}").status_code == 204
        assert client.delete(f"/api/resources/{TestFullScenario._gpu_id}").status_code == 204
        assert client.get(f"/api/resources/{TestFullScenario._alice_id}").status_code == 404
        assert client.get(f"/api/resources/{TestFullScenario._gpu_id}").status_code == 404
