import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app


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


@pytest.fixture(name="project_id")
def project_id_fixture(client):
    resp = client.post("/api/projects/", json={"name": "Test Project"})
    return resp.json()["id"]


@pytest.fixture(name="resource_id")
def resource_id_fixture(client):
    resp = client.post(
        "/api/resources/", json={"name": "Alice", "kind": "human", "capacity": 8.0}
    )
    return resp.json()["id"]


# --- basic CRUD ---

def test_create_task_minimal(client, project_id):
    resp = client.post("/api/tasks/", json={"title": "Write tests", "project_id": project_id})
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Write tests"
    assert body["project_id"] == project_id
    assert body["status"] == "todo"
    assert body["load"] == 1.0
    assert body["resource_id"] is None


def test_create_task_with_resource_and_dates(client, project_id, resource_id):
    resp = client.post(
        "/api/tasks/",
        json={
            "title": "Backend work",
            "project_id": project_id,
            "resource_id": resource_id,
            "start_date": "2026-06-01",
            "end_date": "2026-06-30",
            "load": 6.0,
            "status": "in_progress",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["resource_id"] == resource_id
    assert body["start_date"] == "2026-06-01"
    assert body["end_date"] == "2026-06-30"
    assert body["load"] == 6.0
    assert body["status"] == "in_progress"


def test_list_tasks(client, project_id):
    client.post("/api/tasks/", json={"title": "T1", "project_id": project_id})
    client.post("/api/tasks/", json={"title": "T2", "project_id": project_id})
    resp = client.get("/api/tasks/")
    assert resp.status_code == 200
    titles = [t["title"] for t in resp.json()]
    assert "T1" in titles
    assert "T2" in titles


def test_list_tasks_filter_by_project(client, project_id):
    other = client.post("/api/projects/", json={"name": "Other"}).json()["id"]
    client.post("/api/tasks/", json={"title": "Mine", "project_id": project_id})
    client.post("/api/tasks/", json={"title": "Theirs", "project_id": other})
    resp = client.get(f"/api/tasks/?project_id={project_id}")
    titles = [t["title"] for t in resp.json()]
    assert "Mine" in titles
    assert "Theirs" not in titles


def test_get_task(client, project_id):
    created = client.post("/api/tasks/", json={"title": "Get me", "project_id": project_id}).json()
    resp = client.get(f"/api/tasks/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Get me"


def test_patch_task_title(client, project_id):
    created = client.post("/api/tasks/", json={"title": "Old", "project_id": project_id}).json()
    resp = client.patch(f"/api/tasks/{created['id']}", json={"title": "New"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "New"


def test_patch_task_status(client, project_id):
    created = client.post("/api/tasks/", json={"title": "T", "project_id": project_id}).json()
    resp = client.patch(f"/api/tasks/{created['id']}", json={"status": "in_progress"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


def test_delete_task(client, project_id):
    created = client.post("/api/tasks/", json={"title": "Del", "project_id": project_id}).json()
    assert client.delete(f"/api/tasks/{created['id']}").status_code == 204
    assert client.get(f"/api/tasks/{created['id']}").status_code == 404


def test_full_crud_round_trip(client, project_id, resource_id):
    created = client.post(
        "/api/tasks/",
        json={"title": "Lifecycle", "project_id": project_id, "start_date": "2026-07-01"},
    ).json()
    tid = created["id"]

    patched = client.patch(f"/api/tasks/{tid}", json={"resource_id": resource_id, "status": "in_progress"}).json()
    assert patched["resource_id"] == resource_id
    assert patched["status"] == "in_progress"

    assert client.delete(f"/api/tasks/{tid}").status_code == 204
    assert client.get(f"/api/tasks/{tid}").status_code == 404


# --- assign / unassign resource ---

def test_assign_resource(client, project_id, resource_id):
    task = client.post("/api/tasks/", json={"title": "T", "project_id": project_id}).json()
    resp = client.patch(f"/api/tasks/{task['id']}", json={"resource_id": resource_id})
    assert resp.status_code == 200
    assert resp.json()["resource_id"] == resource_id


def test_unassign_resource(client, project_id, resource_id):
    task = client.post(
        "/api/tasks/",
        json={"title": "T", "project_id": project_id, "resource_id": resource_id},
    ).json()
    resp = client.patch(f"/api/tasks/{task['id']}", json={"resource_id": None})
    assert resp.status_code == 200
    assert resp.json()["resource_id"] is None


# --- date validation ---

def test_create_rejects_end_before_start(client, project_id):
    resp = client.post(
        "/api/tasks/",
        json={
            "title": "Bad dates",
            "project_id": project_id,
            "start_date": "2026-06-30",
            "end_date": "2026-06-01",
        },
    )
    assert resp.status_code == 422


def test_create_same_start_end_ok(client, project_id):
    resp = client.post(
        "/api/tasks/",
        json={"title": "Same day", "project_id": project_id, "start_date": "2026-06-01", "end_date": "2026-06-01"},
    )
    assert resp.status_code == 201


def test_patch_rejects_end_before_existing_start(client, project_id):
    task = client.post(
        "/api/tasks/",
        json={"title": "T", "project_id": project_id, "start_date": "2026-06-15"},
    ).json()
    resp = client.patch(f"/api/tasks/{task['id']}", json={"end_date": "2026-06-01"})
    assert resp.status_code == 422


# --- 404 on missing ID ---

def test_get_missing_task(client):
    assert client.get("/api/tasks/9999").status_code == 404


def test_patch_missing_task(client):
    assert client.patch("/api/tasks/9999", json={"title": "X"}).status_code == 404


def test_delete_missing_task(client):
    assert client.delete("/api/tasks/9999").status_code == 404


# --- reject assignment to nonexistent resource ---

def test_create_task_nonexistent_resource(client, project_id):
    resp = client.post(
        "/api/tasks/",
        json={"title": "T", "project_id": project_id, "resource_id": 9999},
    )
    assert resp.status_code == 404


def test_patch_task_nonexistent_resource(client, project_id):
    task = client.post("/api/tasks/", json={"title": "T", "project_id": project_id}).json()
    resp = client.patch(f"/api/tasks/{task['id']}", json={"resource_id": 9999})
    assert resp.status_code == 404


# --- reject creation under nonexistent project ---

def test_create_task_nonexistent_project(client):
    resp = client.post("/api/tasks/", json={"title": "T", "project_id": 9999})
    assert resp.status_code == 404


# --- validation ---

def test_create_missing_title(client, project_id):
    assert client.post("/api/tasks/", json={"project_id": project_id}).status_code == 422


def test_create_zero_load(client, project_id):
    resp = client.post("/api/tasks/", json={"title": "T", "project_id": project_id, "load": 0.0})
    assert resp.status_code == 422
