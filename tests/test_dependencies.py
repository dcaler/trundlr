"""Multiple dependencies per task: API, deprecated depends_on_id, cycles,
deletion, and the runner waiting for all of them."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.database import get_db
from app.main import app


@pytest.fixture(name="client")
def client_fixture():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def fk_on(dbapi_conn, _):
        dbapi_conn.cursor().execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        def override():
            yield session
        app.dependency_overrides[get_db] = override
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(name="project_id")
def project_id_fixture(client):
    return client.post("/api/projects/", json={"name": "P"}).json()["id"]


def _task(client, project_id, title, **fields):
    resp = client.post("/api/tasks/", json={"title": title, "project_id": project_id, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _patch(client, task_id, **fields):
    return client.patch(f"/api/tasks/{task_id}", json=fields)


def test_create_with_several_dependencies(client, project_id):
    a = _task(client, project_id, "a")
    b = _task(client, project_id, "b")
    c = _task(client, project_id, "c", depends_on_ids=[b["id"], a["id"]])
    assert c["depends_on_ids"] == sorted([a["id"], b["id"]])
    assert c["depends_on_id"] == min(a["id"], b["id"])  # deprecated: lowest id
    got = client.get(f"/api/tasks/{c['id']}").json()
    assert got["depends_on_ids"] == c["depends_on_ids"]
    listed = next(t for t in client.get("/api/tasks/").json() if t["id"] == c["id"])
    assert listed["depends_on_ids"] == c["depends_on_ids"]


def test_deprecated_single_depends_on_id_still_works(client, project_id):
    a = _task(client, project_id, "a")
    b = _task(client, project_id, "b")
    c = _task(client, project_id, "c", depends_on_id=a["id"])
    assert c["depends_on_ids"] == [a["id"]]
    # PATCH with the old field replaces all dependencies with that one.
    _patch(client, c["id"], depends_on_ids=[a["id"], b["id"]])
    resp = _patch(client, c["id"], depends_on_id=b["id"])
    assert resp.json()["depends_on_ids"] == [b["id"]]
    assert _patch(client, c["id"], depends_on_id=None).json()["depends_on_ids"] == []


def test_patch_replaces_and_clears_dependencies(client, project_id):
    a = _task(client, project_id, "a")
    b = _task(client, project_id, "b")
    c = _task(client, project_id, "c", depends_on_ids=[a["id"]])
    assert _patch(client, c["id"], depends_on_ids=[a["id"], b["id"]]).json()["depends_on_ids"] \
        == sorted([a["id"], b["id"]])
    assert _patch(client, c["id"], depends_on_ids=[]).json()["depends_on_ids"] == []


def test_both_fields_at_once_rejected(client, project_id):
    a = _task(client, project_id, "a")
    c = _task(client, project_id, "c")
    resp = _patch(client, c["id"], depends_on_ids=[a["id"]], depends_on_id=a["id"])
    assert resp.status_code == 422


def test_missing_dependency_rejected(client, project_id):
    assert client.post("/api/tasks/", json={"title": "x", "project_id": project_id,
                                            "depends_on_ids": [999999]}).status_code == 404


def test_cycle_through_any_branch_rejected(client, project_id):
    a = _task(client, project_id, "a")
    b = _task(client, project_id, "b")
    c = _task(client, project_id, "c", depends_on_ids=[a["id"], b["id"]])
    d = _task(client, project_id, "d", depends_on_ids=[c["id"]])
    # b → d would close the loop b → d → c → b (via c's second dependency).
    assert _patch(client, b["id"], depends_on_ids=[d["id"]]).status_code == 422
    assert _patch(client, a["id"], depends_on_ids=[a["id"]]).status_code == 422
    # A diamond is fine: e depends on c and d, which share a and b.
    e = _task(client, project_id, "e", depends_on_ids=[c["id"], d["id"]])
    assert e["depends_on_ids"] == sorted([c["id"], d["id"]])


def test_deleting_one_dependency_blocks_task_and_keeps_the_others(client, project_id):
    a = _task(client, project_id, "a")
    b = _task(client, project_id, "b")
    c = _task(client, project_id, "c", depends_on_ids=[a["id"], b["id"]])
    assert client.delete(f"/api/tasks/{a['id']}").status_code == 204
    got = client.get(f"/api/tasks/{c['id']}").json()
    assert got["depends_on_ids"] == [b["id"]]
    assert got["status"] == "blocked" and got["dependency_broken"] is True


def test_deleting_a_task_with_dependencies_removes_its_links(client, project_id):
    a = _task(client, project_id, "a")
    c = _task(client, project_id, "c", depends_on_ids=[a["id"]])
    assert client.delete(f"/api/tasks/{c['id']}").status_code == 204
    assert client.delete(f"/api/tasks/{a['id']}").status_code == 204


def test_open_only_includes_every_done_dependency(client, project_id):
    a = _task(client, project_id, "a", status="done")
    b = _task(client, project_id, "b", status="done")
    _task(client, project_id, "c", depends_on_ids=[a["id"], b["id"]])
    titles = {t["title"] for t in client.get("/api/tasks/?open_only=true").json()}
    assert titles == {"a", "b", "c"}


def test_runner_waits_for_all_dependencies(client, project_id):
    gpu = client.post("/api/resources/", json={
        "name": "GPU", "kind": "gpu", "available_from": "00:00", "available_to": "23:59",
        "available_days": 127}).json()["id"]
    a = _task(client, project_id, "a", status="done")
    b = _task(client, project_id, "b")
    c = _task(client, project_id, "c", command="echo hi", resource_ids=[gpu],
              depends_on_ids=[a["id"], b["id"]])
    resp = client.post(f"/api/runner/{gpu}/claim")
    assert resp.status_code == 204
    assert resp.headers["X-Runner-Idle"].startswith(f"waiting-dep:{c['id']}:{b['id']}:")
    _patch(client, b["id"], status="done")
    resp = client.post(f"/api/runner/{gpu}/claim")
    assert resp.status_code == 200 and resp.json()["id"] == c["id"]
    assert resp.json()["depends_on_ids"] == sorted([a["id"], b["id"]])

