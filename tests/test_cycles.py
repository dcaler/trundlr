"""Tests for cycle templates and instantiation (task-bundle macro).

A cycle template is a reusable, ordered bundle of steps (title + duration +
resources). Instantiating it onto a project creates a numbered, chained batch of
tasks: "<step title> <n>", each depends_on the previous, no dates.
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


@pytest.fixture
def seeded(client):
    """A project, a resource, and a 5-step 'Lit Review' template."""
    project = client.post("/api/projects/", json={"name": "Paper"}).json()
    resource = client.post("/api/resources/", json={"name": "Cale", "kind": "human"}).json()
    template = client.post("/api/cycle-templates/", json={"name": "Lit Review"}).json()
    for i, title in enumerate(["Init", "Gather", "Collect", "Draft", "Review"]):
        client.post(
            f"/api/cycle-templates/{template['id']}/steps",
            json={"title": title, "duration": 2, "resource_ids": [resource["id"]], "position": i},
        )
    return client, project["id"], resource["id"], template["id"]


# ── template + step CRUD ──────────────────────────────────────────────────────

class TestTemplateCrud:
    def test_create_and_list(self, client):
        client.post("/api/cycle-templates/", json={"name": "A"})
        client.post("/api/cycle-templates/", json={"name": "B"})
        names = [t["name"] for t in client.get("/api/cycle-templates/").json()]
        assert names == ["A", "B"]  # ordered by name

    def test_steps_returned_in_position_order(self, seeded):
        client, _, _, tid = seeded
        t = next(t for t in client.get("/api/cycle-templates/").json() if t["id"] == tid)
        assert [s["title"] for s in t["steps"]] == ["Init", "Gather", "Collect", "Draft", "Review"]
        assert t["steps"][0]["resource_ids"] and t["steps"][0]["duration"] == 2.0

    def test_rename_template(self, seeded):
        client, _, _, tid = seeded
        client.patch(f"/api/cycle-templates/{tid}", json={"name": "Renamed"})
        t = next(t for t in client.get("/api/cycle-templates/").json() if t["id"] == tid)
        assert t["name"] == "Renamed"

    def test_update_step(self, seeded):
        client, _, _, tid = seeded
        step = client.get("/api/cycle-templates/").json()[0]["steps"][0]
        r = client.patch(f"/api/cycle-templates/steps/{step['id']}",
                         json={"title": "Kickoff", "duration": 3})
        assert r.status_code == 200
        assert r.json()["title"] == "Kickoff" and r.json()["duration"] == 3.0

    def test_delete_step(self, seeded):
        client, _, _, tid = seeded
        step = client.get("/api/cycle-templates/").json()[0]["steps"][0]
        assert client.delete(f"/api/cycle-templates/steps/{step['id']}").status_code == 204
        titles = [s["title"] for s in client.get("/api/cycle-templates/").json()[0]["steps"]]
        assert "Init" not in titles

    def test_delete_template_removes_steps(self, seeded):
        client, _, _, tid = seeded
        assert client.delete(f"/api/cycle-templates/{tid}").status_code == 204
        assert client.get("/api/cycle-templates/").json() == []

    def test_add_step_unknown_resource_404(self, seeded):
        client, _, _, tid = seeded
        r = client.post(f"/api/cycle-templates/{tid}/steps",
                        json={"title": "X", "resource_ids": [9999]})
        assert r.status_code == 404


# ── instantiation ─────────────────────────────────────────────────────────────

class TestInstantiate:
    def test_creates_chained_numbered_tasks(self, seeded):
        client, pid, rid, tid = seeded
        tasks = client.post(f"/api/cycle-templates/{tid}/instantiate",
                            json={"project_id": pid}).json()
        assert [t["title"] for t in tasks] == ["Init 1", "Gather 1", "Collect 1", "Draft 1", "Review 1"]
        # first step has no dependency; each subsequent depends on the previous
        assert tasks[0]["depends_on_id"] is None
        assert all(tasks[i]["depends_on_id"] == tasks[i - 1]["id"] for i in range(1, len(tasks)))
        # duration + resources copied from the template; no dates
        assert all(t["duration"] == 2.0 for t in tasks)
        assert all(t["resource_ids"] == [rid] for t in tasks)
        assert all(t["start_date"] is None and t["end_date"] is None for t in tasks)

    def test_second_cycle_increments_and_starts_independent(self, seeded):
        client, pid, _, tid = seeded
        client.post(f"/api/cycle-templates/{tid}/instantiate", json={"project_id": pid})
        c2 = client.post(f"/api/cycle-templates/{tid}/instantiate", json={"project_id": pid}).json()
        assert [t["title"] for t in c2] == ["Init 2", "Gather 2", "Collect 2", "Draft 2", "Review 2"]
        assert c2[0]["depends_on_id"] is None  # cycle 2 does NOT chain to cycle 1

    def test_number_is_highest_plus_one(self, seeded):
        """A pre-existing 'Init 5' forces the whole next cycle to number 6."""
        client, pid, _, tid = seeded
        client.post("/api/tasks/", json={"title": "Init 5", "project_id": pid})
        c = client.post(f"/api/cycle-templates/{tid}/instantiate", json={"project_id": pid}).json()
        assert [t["title"] for t in c] == ["Init 6", "Gather 6", "Collect 6", "Draft 6", "Review 6"]

    def test_numbering_scoped_per_project(self, seeded):
        client, pid, _, tid = seeded
        other = client.post("/api/projects/", json={"name": "Other"}).json()
        client.post(f"/api/cycle-templates/{tid}/instantiate", json={"project_id": pid})
        c_other = client.post(f"/api/cycle-templates/{tid}/instantiate",
                             json={"project_id": other["id"]}).json()
        assert c_other[0]["title"] == "Init 1"  # other project starts fresh

    def test_empty_template_422(self, client):
        pid = client.post("/api/projects/", json={"name": "P"}).json()["id"]
        tid = client.post("/api/cycle-templates/", json={"name": "Empty"}).json()["id"]
        r = client.post(f"/api/cycle-templates/{tid}/instantiate", json={"project_id": pid})
        assert r.status_code == 422

    def test_unknown_project_404(self, seeded):
        client, _, _, tid = seeded
        r = client.post(f"/api/cycle-templates/{tid}/instantiate", json={"project_id": 9999})
        assert r.status_code == 404

    def test_unknown_template_404(self, client):
        pid = client.post("/api/projects/", json={"name": "P"}).json()["id"]
        r = client.post("/api/cycle-templates/9999/instantiate", json={"project_id": pid})
        assert r.status_code == 404
