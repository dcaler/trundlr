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

def test_create_human_resource(client):
    resp = client.post(
        "/api/resources/", json={"name": "Alice", "kind": "human", "capacity": 8.0}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Alice"
    assert body["kind"] == "human"
    assert body["capacity"] == 8.0
    assert "id" in body


def test_create_cpu_resource(client):
    resp = client.post(
        "/api/resources/", json={"name": "CPU Node", "kind": "cpu", "capacity": 4.0}
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "cpu"


def test_create_gpu_resource(client):
    resp = client.post(
        "/api/resources/", json={"name": "GPU Node", "kind": "gpu", "capacity": 2.0}
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "gpu"


def test_list_resources(client):
    client.post("/api/resources/", json={"name": "R1", "kind": "human", "capacity": 8.0})
    client.post("/api/resources/", json={"name": "R2", "kind": "cpu", "capacity": 4.0})
    resp = client.get("/api/resources/")
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()]
    assert "R1" in names
    assert "R2" in names


def test_get_resource(client):
    created = client.post(
        "/api/resources/", json={"name": "Bob", "kind": "human", "capacity": 6.0}
    ).json()
    resp = client.get(f"/api/resources/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Bob"


def test_patch_resource_name(client):
    created = client.post(
        "/api/resources/", json={"name": "Old", "kind": "human", "capacity": 8.0}
    ).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"name": "New"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"
    assert resp.json()["capacity"] == 8.0


def test_patch_resource_capacity(client):
    created = client.post(
        "/api/resources/", json={"name": "GPU1", "kind": "gpu", "capacity": 2.0}
    ).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"capacity": 4.0})
    assert resp.status_code == 200
    assert resp.json()["capacity"] == 4.0
    assert resp.json()["name"] == "GPU1"


def test_delete_resource(client):
    created = client.post(
        "/api/resources/", json={"name": "Temp", "kind": "cpu", "capacity": 1.0}
    ).json()
    assert client.delete(f"/api/resources/{created['id']}").status_code == 204
    assert client.get(f"/api/resources/{created['id']}").status_code == 404


def test_full_crud_round_trip(client):
    created = client.post(
        "/api/resources/", json={"name": "Worker", "kind": "human", "capacity": 8.0}
    ).json()
    rid = created["id"]

    assert client.get(f"/api/resources/{rid}").json()["name"] == "Worker"

    updated = client.patch(f"/api/resources/{rid}", json={"capacity": 6.0}).json()
    assert updated["capacity"] == 6.0
    assert updated["kind"] == "human"

    assert client.delete(f"/api/resources/{rid}").status_code == 204
    assert client.get(f"/api/resources/{rid}").status_code == 404


# --- 404 on missing ID ---

def test_get_missing_resource(client):
    assert client.get("/api/resources/9999").status_code == 404


def test_patch_missing_resource(client):
    assert client.patch("/api/resources/9999", json={"name": "X"}).status_code == 404


def test_delete_missing_resource(client):
    assert client.delete("/api/resources/9999").status_code == 404


# --- invalid kind ---

def test_create_invalid_kind(client):
    resp = client.post(
        "/api/resources/", json={"name": "X", "kind": "robot", "capacity": 1.0}
    )
    assert resp.status_code == 422


# --- capacity <= 0 ---

def test_create_zero_capacity(client):
    resp = client.post(
        "/api/resources/", json={"name": "X", "kind": "human", "capacity": 0.0}
    )
    assert resp.status_code == 422


def test_create_negative_capacity(client):
    resp = client.post(
        "/api/resources/", json={"name": "X", "kind": "human", "capacity": -1.0}
    )
    assert resp.status_code == 422


def test_patch_zero_capacity(client):
    created = client.post(
        "/api/resources/", json={"name": "Y", "kind": "cpu", "capacity": 4.0}
    ).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"capacity": 0.0})
    assert resp.status_code == 422


def test_create_missing_required_fields(client):
    assert client.post("/api/resources/", json={"name": "X"}).status_code == 422
    assert client.post("/api/resources/", json={"kind": "human", "capacity": 4.0}).status_code == 422
