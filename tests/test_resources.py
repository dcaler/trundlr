import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app

HUMAN = {"name": "Alice", "kind": "human", "available_from": "09:00", "available_to": "17:00", "available_days": 31}


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
    resp = client.post("/api/resources/", json=HUMAN)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Alice"
    assert body["kind"] == "human"
    assert body["capacity"] is None
    assert body["available_from"] == "09:00"
    assert body["available_to"] == "17:00"
    assert body["available_days"] == 31
    assert "id" in body


def test_create_cpu_resource(client):
    resp = client.post(
        "/api/resources/", json={"name": "CPU Node", "kind": "cpu", "capacity": 4.0}
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "cpu"
    assert resp.json()["capacity"] == 4.0


def test_create_gpu_resource(client):
    resp = client.post(
        "/api/resources/", json={"name": "GPU Node", "kind": "gpu", "capacity": 2.0}
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "gpu"


def test_list_resources(client):
    client.post("/api/resources/", json={**HUMAN, "name": "R1"})
    client.post("/api/resources/", json={"name": "R2", "kind": "cpu", "capacity": 4.0})
    resp = client.get("/api/resources/")
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()]
    assert "R1" in names
    assert "R2" in names


def test_get_resource(client):
    created = client.post("/api/resources/", json={**HUMAN, "name": "Bob"}).json()
    resp = client.get(f"/api/resources/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Bob"


def test_patch_resource_name(client):
    created = client.post("/api/resources/", json=HUMAN).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"name": "New"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"
    assert resp.json()["available_from"] == "09:00"


def test_patch_resource_capacity(client):
    created = client.post(
        "/api/resources/", json={"name": "GPU1", "kind": "gpu", "capacity": 2.0}
    ).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"capacity": 4.0})
    assert resp.status_code == 200
    assert resp.json()["capacity"] == 4.0
    assert resp.json()["name"] == "GPU1"


def test_patch_human_availability(client):
    created = client.post("/api/resources/", json=HUMAN).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"available_to": "15:00"})
    assert resp.status_code == 200
    assert resp.json()["available_to"] == "15:00"


def test_delete_resource(client):
    created = client.post(
        "/api/resources/", json={"name": "Temp", "kind": "cpu", "capacity": 1.0}
    ).json()
    assert client.delete(f"/api/resources/{created['id']}").status_code == 204
    assert client.get(f"/api/resources/{created['id']}").status_code == 404


def test_full_crud_round_trip(client):
    created = client.post("/api/resources/", json=HUMAN).json()
    rid = created["id"]

    assert client.get(f"/api/resources/{rid}").json()["name"] == "Alice"

    updated = client.patch(f"/api/resources/{rid}", json={"available_to": "15:00"}).json()
    assert updated["available_to"] == "15:00"
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


# --- human with capacity rejected ---

def test_create_human_with_capacity_rejected(client):
    resp = client.post(
        "/api/resources/", json={"name": "X", "kind": "human", "capacity": 8.0}
    )
    assert resp.status_code == 422


# --- capacity <= 0 for compute resources ---

def test_create_zero_capacity(client):
    resp = client.post(
        "/api/resources/", json={"name": "X", "kind": "cpu", "capacity": 0.0}
    )
    assert resp.status_code == 422


def test_create_negative_capacity(client):
    resp = client.post(
        "/api/resources/", json={"name": "X", "kind": "cpu", "capacity": -1.0}
    )
    assert resp.status_code == 422


def test_patch_zero_capacity(client):
    created = client.post(
        "/api/resources/", json={"name": "Y", "kind": "cpu", "capacity": 4.0}
    ).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"capacity": 0.0})
    assert resp.status_code == 422


# --- human missing availability fields ---

def test_create_human_missing_availability(client):
    resp = client.post(
        "/api/resources/", json={"name": "X", "kind": "human"}
    )
    assert resp.status_code == 422


def test_create_human_invalid_time_format(client):
    resp = client.post(
        "/api/resources/",
        json={"name": "X", "kind": "human", "available_from": "9am",
              "available_to": "17:00", "available_days": 31},
    )
    assert resp.status_code == 422


def test_create_human_end_before_start(client):
    resp = client.post(
        "/api/resources/",
        json={"name": "X", "kind": "human", "available_from": "17:00",
              "available_to": "09:00", "available_days": 31},
    )
    assert resp.status_code == 422


def test_create_missing_required_fields(client):
    assert client.post("/api/resources/", json={"name": "X"}).status_code == 422
    assert client.post("/api/resources/", json={"kind": "human"}).status_code == 422
