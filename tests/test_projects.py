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


# --- CRUD round-trip ---

def test_create_project(client):
    resp = client.post("/api/projects/", json={"name": "Alpha", "description": "First"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Alpha"
    assert body["description"] == "First"
    assert "id" in body
    assert "created_at" in body


def test_list_projects(client):
    client.post("/api/projects/", json={"name": "P1"})
    client.post("/api/projects/", json={"name": "P2"})
    resp = client.get("/api/projects/")
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert "P1" in names
    assert "P2" in names


def test_get_project(client):
    created = client.post("/api/projects/", json={"name": "Beta"}).json()
    resp = client.get(f"/api/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Beta"


def test_patch_project(client):
    created = client.post("/api/projects/", json={"name": "Old Name"}).json()
    resp = client.patch(
        f"/api/projects/{created['id']}", json={"name": "New Name"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"


def test_patch_project_partial(client):
    created = client.post(
        "/api/projects/", json={"name": "Keep", "description": "Desc"}
    ).json()
    resp = client.patch(
        f"/api/projects/{created['id']}", json={"description": "Updated"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Keep"
    assert body["description"] == "Updated"


def test_delete_project(client):
    created = client.post("/api/projects/", json={"name": "Delete Me"}).json()
    resp = client.delete(f"/api/projects/{created['id']}")
    assert resp.status_code == 204
    assert client.get(f"/api/projects/{created['id']}").status_code == 404


def test_full_crud_round_trip(client):
    created = client.post(
        "/api/projects/", json={"name": "Lifecycle", "description": "Test"}
    ).json()
    pid = created["id"]

    assert client.get(f"/api/projects/{pid}").json()["name"] == "Lifecycle"

    updated = client.patch(f"/api/projects/{pid}", json={"name": "Renamed"}).json()
    assert updated["name"] == "Renamed"
    assert updated["description"] == "Test"

    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404


# --- 404 on missing ID ---

def test_get_missing_project(client):
    assert client.get("/api/projects/9999").status_code == 404


def test_patch_missing_project(client):
    assert client.patch("/api/projects/9999", json={"name": "X"}).status_code == 404


def test_delete_missing_project(client):
    assert client.delete("/api/projects/9999").status_code == 404


# --- 422 on bad payload ---

def test_create_missing_name(client):
    resp = client.post("/api/projects/", json={})
    assert resp.status_code == 422


def test_create_null_name(client):
    resp = client.post("/api/projects/", json={"name": None})
    assert resp.status_code == 422
