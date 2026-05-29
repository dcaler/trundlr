"""Integration tests for project & task management (Step 4.2).

Covers the full flow the plan specifies: create project → add tasks →
assign to resource → change status → verify persistence → delete cascades.

Playwright is not used (no browser installed); these tests exercise the same
API surface the JS UI calls and assert the same "persists via the API"
guarantee.
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

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

def create_project(client, name="Alpha", description=None):
    return client.post("/api/projects/", json={"name": name, "description": description}).json()

def create_resource(client, name="Alice", kind="human", capacity=None):
    if kind == "human":
        body = {"name": name, "kind": kind,
                "available_from": "09:00", "available_to": "17:00", "available_days": 31}
    else:
        body = {"name": name, "kind": kind, "capacity": capacity}
    return client.post("/api/resources/", json=body).json()

def create_task(client, project_id, title="Task 1", **kwargs):
    return client.post("/api/tasks/", json={"title": title, "project_id": project_id, **kwargs}).json()


# ── Full flow: create → assign → status → persist ────────────────────────────

def test_create_project_persists(client):
    p = create_project(client, "Alpha", "first project")
    fetched = client.get(f"/api/projects/{p['id']}").json()
    assert fetched["name"] == "Alpha"
    assert fetched["description"] == "first project"


def test_add_task_to_project(client):
    p = create_project(client)
    t = create_task(client, p["id"], title="Design")
    assert t["title"] == "Design"
    assert t["project_id"] == p["id"]
    assert t["status"] == "todo"


def test_task_list_filtered_by_project(client):
    p1 = create_project(client, "P1")
    p2 = create_project(client, "P2")
    create_task(client, p1["id"], title="T1")
    create_task(client, p1["id"], title="T2")
    create_task(client, p2["id"], title="T3")

    tasks_p1 = client.get(f"/api/tasks/?project_id={p1['id']}").json()
    tasks_p2 = client.get(f"/api/tasks/?project_id={p2['id']}").json()

    assert {t["title"] for t in tasks_p1} == {"T1", "T2"}
    assert {t["title"] for t in tasks_p2} == {"T3"}


def test_assign_resource_to_task(client):
    p = create_project(client)
    r = create_resource(client, "Bob", "human", 8.0)
    t = create_task(client, p["id"], title="Code review")

    updated = client.patch(
        f"/api/tasks/{t['id']}",
        json={"resource_id": r["id"]},
    ).json()
    assert updated["resource_id"] == r["id"]

    # Verify persistence via fresh GET
    fetched = client.get(f"/api/tasks/{t['id']}").json()
    assert fetched["resource_id"] == r["id"]


def test_assign_with_dates_and_load(client):
    p = create_project(client)
    r = create_resource(client, "GPU Node", "gpu", 4.0)
    t = create_task(
        client, p["id"],
        title="Training run",
        resource_id=r["id"],
        start_date="2026-06-01",
        end_date="2026-06-07",
        load=2.0,
    )
    assert t["resource_id"] == r["id"]
    assert t["start_date"].startswith("2026-06-01")
    assert t["end_date"].startswith("2026-06-07")
    assert t["load"] == pytest.approx(2.0)


def test_status_change_persists(client):
    p = create_project(client)
    t = create_task(client, p["id"])
    assert t["status"] == "todo"

    client.patch(f"/api/tasks/{t['id']}", json={"status": "in_progress"})
    assert client.get(f"/api/tasks/{t['id']}").json()["status"] == "in_progress"

    client.patch(f"/api/tasks/{t['id']}", json={"status": "done"})
    assert client.get(f"/api/tasks/{t['id']}").json()["status"] == "done"


def test_unassign_resource(client):
    p = create_project(client)
    r = create_resource(client)
    t = create_task(client, p["id"], resource_id=r["id"])
    assert t["resource_id"] == r["id"]

    client.patch(f"/api/tasks/{t['id']}", json={"resource_id": None})
    assert client.get(f"/api/tasks/{t['id']}").json()["resource_id"] is None


# ── Cascade / referential integrity ──────────────────────────────────────────

def test_delete_project_cascades_tasks(client):
    p = create_project(client)
    t1 = create_task(client, p["id"], title="T1")
    t2 = create_task(client, p["id"], title="T2")

    resp = client.delete(f"/api/projects/{p['id']}")
    assert resp.status_code == 204

    # Tasks must be gone
    assert client.get(f"/api/tasks/{t1['id']}").status_code == 404
    assert client.get(f"/api/tasks/{t2['id']}").status_code == 404


def test_delete_resource_unassigns_tasks(client):
    p = create_project(client)
    r = create_resource(client)
    t = create_task(client, p["id"], resource_id=r["id"])
    assert t["resource_id"] == r["id"]

    resp = client.delete(f"/api/resources/{r['id']}")
    assert resp.status_code == 204

    # Task still exists but is unassigned
    fetched = client.get(f"/api/tasks/{t['id']}").json()
    assert fetched["resource_id"] is None


def test_full_flow(client):
    """End-to-end: create project + resource → add task → assign → verify."""
    project = create_project(client, "Website Redesign", "Q3 initiative")
    resource = create_resource(client, "Alice", "human", 8.0)

    task = create_task(
        client,
        project["id"],
        title="Wireframes",
        resource_id=resource["id"],
        start_date="2026-07-01",
        end_date="2026-07-05",
        load=4.0,
        status="in_progress",
    )

    # Assert all fields
    assert task["project_id"] == project["id"]
    assert task["resource_id"] == resource["id"]
    assert task["start_date"].startswith("2026-07-01")
    assert task["load"] == pytest.approx(4.0)
    assert task["status"] == "in_progress"

    # Task appears in project task list
    tasks = client.get(f"/api/tasks/?project_id={project['id']}").json()
    assert any(t["id"] == task["id"] for t in tasks)

    # Mark done and verify
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "done"
