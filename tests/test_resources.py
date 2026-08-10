import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app

HUMAN = {"name": "Alice", "kind": "human", "available_from": "09:00", "available_to": "17:00", "available_days": 31}
CPU   = {"name": "CPU Node", "kind": "cpu", "available_from": "00:00", "available_to": "23:59", "available_days": 127}
GPU   = {"name": "GPU Node", "kind": "gpu", "available_from": "00:00", "available_to": "23:59", "available_days": 127}


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
    assert body["available_from"] == "09:00"
    assert body["available_to"] == "17:00"
    assert body["available_days"] == 31
    assert "id" in body


def test_create_cpu_resource(client):
    resp = client.post("/api/resources/", json=CPU)
    assert resp.status_code == 201
    assert resp.json()["kind"] == "cpu"
    assert resp.json()["available_days"] == 127


def test_create_gpu_resource(client):
    resp = client.post("/api/resources/", json=GPU)
    assert resp.status_code == 201
    assert resp.json()["kind"] == "gpu"


def test_list_resources(client):
    client.post("/api/resources/", json={**HUMAN, "name": "R1"})
    client.post("/api/resources/", json={**CPU, "name": "R2"})
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


def test_patch_resource_availability(client):
    created = client.post("/api/resources/", json=GPU).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"available_to": "22:00"})
    assert resp.status_code == 200
    assert resp.json()["available_to"] == "22:00"
    assert resp.json()["name"] == "GPU Node"


def test_patch_human_availability(client):
    created = client.post("/api/resources/", json=HUMAN).json()
    resp = client.patch(f"/api/resources/{created['id']}", json={"available_to": "15:00"})
    assert resp.status_code == 200
    assert resp.json()["available_to"] == "15:00"


def test_delete_resource(client):
    created = client.post("/api/resources/", json=CPU).json()
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
    resp = client.post("/api/resources/", json={"name": "X", "kind": "robot"})
    assert resp.status_code == 422


# --- availability validation ---

def test_create_invalid_time_format(client):
    resp = client.post(
        "/api/resources/",
        json={"name": "X", "kind": "human", "available_from": "9am",
              "available_to": "17:00", "available_days": 31},
    )
    assert resp.status_code == 422


def test_create_end_before_start(client):
    resp = client.post(
        "/api/resources/",
        json={"name": "X", "kind": "human", "available_from": "17:00",
              "available_to": "09:00", "available_days": 31},
    )
    assert resp.status_code == 422


def test_create_invalid_available_days(client):
    resp = client.post(
        "/api/resources/",
        json={"name": "X", "kind": "gpu", "available_from": "09:00",
              "available_to": "17:00", "available_days": 0},
    )
    assert resp.status_code == 422


def test_create_missing_required_fields(client):
    assert client.post("/api/resources/", json={"name": "X"}).status_code == 422
    assert client.post("/api/resources/", json={"kind": "human"}).status_code == 422


def test_create_defaults_applied(client):
    # All kinds have defaults; just name+kind is enough.
    resp = client.post("/api/resources/", json={"name": "Bare", "kind": "cpu"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["available_from"] == "09:00"
    assert body["available_to"] == "17:00"
    assert body["available_days"] == 31


# ── Windows ───────────────────────────────────────────────────────────────────

class TestWindows:
    def _resource(self, client):
        return client.post("/api/resources/", json=HUMAN).json()

    def test_list_empty(self, client):
        r = self._resource(client)
        resp = client.get(f"/api/resources/{r['id']}/windows")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_and_list(self, client):
        r = self._resource(client)
        payload = {"day_of_week": 0, "from_time": "09:00", "to_time": "12:00"}
        resp = client.post(f"/api/resources/{r['id']}/windows", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["day_of_week"] == 0
        assert body["from_time"] == "09:00"
        assert body["to_time"] == "12:00"
        assert body["resource_id"] == r["id"]

        windows = client.get(f"/api/resources/{r['id']}/windows").json()
        assert len(windows) == 1

    def test_delete_window(self, client):
        r = self._resource(client)
        w = client.post(f"/api/resources/{r['id']}/windows",
                        json={"day_of_week": 1, "from_time": "09:00", "to_time": "17:00"}).json()
        assert client.delete(f"/api/resources/{r['id']}/windows/{w['id']}").status_code == 204
        assert client.get(f"/api/resources/{r['id']}/windows").json() == []

    def test_404_on_missing_resource(self, client):
        assert client.get("/api/resources/9999/windows").status_code == 404
        assert client.post("/api/resources/9999/windows",
                           json={"day_of_week": 0, "from_time": "09:00", "to_time": "17:00"}).status_code == 404

    def test_404_on_missing_window(self, client):
        r = self._resource(client)
        assert client.delete(f"/api/resources/{r['id']}/windows/9999").status_code == 404

    def test_invalid_day_of_week(self, client):
        r = self._resource(client)
        resp = client.post(f"/api/resources/{r['id']}/windows",
                           json={"day_of_week": 7, "from_time": "09:00", "to_time": "17:00"})
        assert resp.status_code == 422

    def test_to_before_from_rejected(self, client):
        r = self._resource(client)
        resp = client.post(f"/api/resources/{r['id']}/windows",
                           json={"day_of_week": 0, "from_time": "17:00", "to_time": "09:00"})
        assert resp.status_code == 422

    def test_deleted_with_resource(self, client):
        r = self._resource(client)
        client.post(f"/api/resources/{r['id']}/windows",
                    json={"day_of_week": 0, "from_time": "09:00", "to_time": "17:00"})
        client.delete(f"/api/resources/{r['id']}")
        assert client.get(f"/api/resources/{r['id']}/windows").status_code == 404


# ── Blockouts ─────────────────────────────────────────────────────────────────

class TestBlockouts:
    def _resource(self, client):
        return client.post("/api/resources/", json=HUMAN).json()

    def test_list_empty(self, client):
        r = self._resource(client)
        resp = client.get(f"/api/resources/{r['id']}/blockouts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_full_day_blockout(self, client):
        r = self._resource(client)
        payload = {"start_date": "2026-06-15", "end_date": "2026-06-20"}
        resp = client.post(f"/api/resources/{r['id']}/blockouts", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["start_date"] == "2026-06-15"
        assert body["end_date"] == "2026-06-20"
        assert body["from_time"] is None
        assert body["note"] is None

    def test_create_partial_day_blockout(self, client):
        r = self._resource(client)
        payload = {
            "start_date": "2026-06-15", "end_date": "2026-06-15",
            "from_time": "12:00", "to_time": "13:00",
            "note": "Lunch",
        }
        resp = client.post(f"/api/resources/{r['id']}/blockouts", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["from_time"] == "12:00"
        assert body["to_time"] == "13:00"
        assert body["note"] == "Lunch"

    def test_delete_blockout(self, client):
        r = self._resource(client)
        b = client.post(f"/api/resources/{r['id']}/blockouts",
                        json={"start_date": "2026-07-01", "end_date": "2026-07-05"}).json()
        assert client.delete(f"/api/resources/{r['id']}/blockouts/{b['id']}").status_code == 204
        assert client.get(f"/api/resources/{r['id']}/blockouts").json() == []

    def test_end_before_start_rejected(self, client):
        r = self._resource(client)
        resp = client.post(f"/api/resources/{r['id']}/blockouts",
                           json={"start_date": "2026-06-20", "end_date": "2026-06-15"})
        assert resp.status_code == 422

    def test_404_on_missing_resource(self, client):
        assert client.get("/api/resources/9999/blockouts").status_code == 404

    def test_deleted_with_resource(self, client):
        r = self._resource(client)
        client.post(f"/api/resources/{r['id']}/blockouts",
                    json={"start_date": "2026-06-15", "end_date": "2026-06-20"})
        client.delete(f"/api/resources/{r['id']}")
        assert client.get(f"/api/resources/{r['id']}/blockouts").status_code == 404
