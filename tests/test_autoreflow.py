"""Keeping the plan honest: overrunning tasks stretch, finishing a task re-flows,
the background re-flow runs on its timer, and "could not be scheduled" emails
only go out when the list changes.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.reflow as reflow
from app.database import create_db_and_tables, get_db, get_engine
from app.main import app
from app.models import AppSettings


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


@pytest.fixture(autouse=True)
def reset_notified():
    reflow._last_notified = None
    yield
    reflow._last_notified = None


_ALWAYS = {"available_from": "00:00", "available_to": "23:59", "available_days": 127}


def _gpu(client, name, spans_ids=()):
    resp = client.post("/api/resources/", json={
        "name": name, "kind": "gpu", **_ALWAYS, "spans_ids": list(spans_ids),
    })
    return resp.json()["id"]


@pytest.fixture(name="project_id")
def project_id_fixture(client):
    return client.post("/api/projects/", json={"name": "P"}).json()["id"]


def _task(client, project_id, rid, **extra):
    resp = client.post("/api/tasks/", json={
        "title": "t", "project_id": project_id, "resource_ids": [rid], "command": "echo hi", **extra,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _now():
    # No settings row → the app timezone is UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get(client, task_id):
    return client.get(f"/api/tasks/{task_id}").json()


def _make_overrun(client, task_id):
    """Pretend a running task was planned to end an hour ago."""
    now = _now().replace(microsecond=0)
    client.patch(f"/api/tasks/{task_id}", json={
        "start_date": (now - timedelta(hours=3)).isoformat(),
        "end_date": (now - timedelta(hours=1)).isoformat(),
    })


# ── Overrunning tasks ────────────────────────────────────────────────────────

def test_reflow_stretches_an_overrunning_task(client, project_id):
    gpu = _gpu(client, "GPU")
    a = _task(client, project_id, gpu, duration=1)
    assert client.post(f"/api/runner/{gpu}/claim").status_code == 200
    _make_overrun(client, a)

    result = client.post("/api/schedule/reflow").json()
    end = datetime.fromisoformat(_get(client, a)["end_date"])
    assert result["changed"] >= 1
    assert abs(end - (_now() + timedelta(hours=1))) < timedelta(minutes=2)
    assert _get(client, a)["status"] == "in_progress"


def test_overrun_does_not_make_a_dual_task_due_early(client, project_id):
    gpu0 = _gpu(client, "GPU 0")
    gpu1 = _gpu(client, "GPU 1")
    dual = _gpu(client, "GPU 0+1", spans_ids=[gpu0, gpu1])
    long_job = _task(client, project_id, gpu1, duration=1)
    assert client.post(f"/api/runner/{gpu1}/claim").status_code == 200
    _make_overrun(client, long_job)

    _task(client, project_id, dual, duration=7)  # creating it re-flows
    waiting = _task(client, project_id, gpu0, duration=1)

    # The dual task is planned after the stretched overrun, so it isn't due
    # yet and GPU 0 keeps working instead of draining for it.
    resp = client.post(f"/api/runner/{gpu0}/claim")
    assert resp.status_code == 200
    assert resp.json()["id"] == waiting


def test_finished_tasks_are_not_stretched(client, project_id):
    gpu = _gpu(client, "GPU")
    a = _task(client, project_id, gpu, duration=1)
    client.post(f"/api/runner/{gpu}/claim")
    _make_overrun(client, a)
    client.patch(f"/api/tasks/{a}", json={"status": "done"})
    before = _get(client, a)["end_date"]
    client.post("/api/schedule/reflow")
    assert _get(client, a)["end_date"] == before


# ── Re-flow when a task finishes ─────────────────────────────────────────────

def test_finishing_early_pulls_dependent_work_forward(client, project_id):
    gpu = _gpu(client, "GPU")
    a = _task(client, project_id, gpu, duration=10)
    assert client.post(f"/api/runner/{gpu}/claim").status_code == 200
    b = _task(client, project_id, gpu, duration=1, depends_on_id=a)
    assert datetime.fromisoformat(_get(client, b)["start_date"]) > _now() + timedelta(hours=9)

    client.patch(f"/api/tasks/{a}", json={
        "status": "done", "end_date": _now().replace(microsecond=0).isoformat(),
    })
    assert datetime.fromisoformat(_get(client, b)["start_date"]) < _now() + timedelta(minutes=5)


# ── Change-only notifications ────────────────────────────────────────────────

def test_unschedulable_email_only_when_the_list_changes(client, project_id, monkeypatch):
    sent = []
    monkeypatch.setattr(reflow, "send_notification", lambda *a, **k: sent.append(a[1]))
    gpu = _gpu(client, "GPU")
    blockout = {"start_date": date.today().isoformat(),
                "end_date": (date.today() + timedelta(days=400)).isoformat()}
    bid = client.post(f"/api/resources/{gpu}/blockouts", json=blockout).json()["id"]

    _task(client, project_id, gpu)               # re-flow: newly unschedulable
    assert sent == ["Tasks could not be scheduled"]
    client.post("/api/schedule/reflow")           # same list: silent
    client.post("/api/schedule/reflow")
    assert len(sent) == 1

    client.delete(f"/api/resources/{gpu}/blockouts/{bid}")
    client.post("/api/schedule/reflow")           # now schedulable: silent
    assert len(sent) == 1
    client.post(f"/api/resources/{gpu}/blockouts", json=blockout)
    client.post("/api/schedule/reflow")           # unschedulable again: email
    assert len(sent) == 2


# ── Background re-flow ───────────────────────────────────────────────────────

def test_auto_reflow_defaults_to_an_hour(client):
    assert client.get("/api/settings/").json()["auto_reflow_minutes"] == 60


@pytest.mark.parametrize("bad", [-1, 1441])
def test_auto_reflow_bounds(client, bad):
    assert client.patch("/api/settings/", json={"auto_reflow_minutes": bad}).status_code == 422


@pytest.fixture(name="file_engine")
def file_engine_fixture(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'auto.db'}")
    create_db_and_tables(engine)
    return engine


def _count_reflows(monkeypatch):
    runs = []
    real = reflow.run_reflow
    monkeypatch.setattr(reflow, "run_reflow", lambda s: runs.append(1) or real(s))
    return runs


def test_auto_reflow_tick_runs_only_when_due(file_engine, monkeypatch):
    runs = _count_reflows(monkeypatch)
    assert reflow.auto_reflow_tick(file_engine, last_run=0, now=59 * 60) == 0
    assert runs == []
    assert reflow.auto_reflow_tick(file_engine, last_run=0, now=60 * 60) == 60 * 60
    assert runs == [1]


def test_auto_reflow_tick_follows_the_setting(file_engine, monkeypatch):
    runs = _count_reflows(monkeypatch)
    with Session(file_engine) as s:
        s.add(AppSettings(auto_reflow_minutes=0))
        s.commit()
    assert reflow.auto_reflow_tick(file_engine, last_run=0, now=10 ** 6) == 0
    assert runs == []


# ── Age priority ─────────────────────────────────────────────────────────────

def _row(session, task_id):
    from app.models import Task
    session.expire_all()
    return session.get(Task, task_id)


def test_ready_tasks_get_eligible_since_and_waiting_ones_do_not(client, session, project_id):
    gpu = _gpu(client, "GPU")
    a = _task(client, project_id, gpu)
    b = _task(client, project_id, gpu, depends_on_id=a)
    assert _row(session, a).eligible_since is not None
    assert _row(session, b).eligible_since is None
    client.patch(f"/api/tasks/{a}", json={"status": "done"})
    assert _row(session, a).eligible_since is None
    assert _row(session, b).eligible_since is not None


def _age(session, task_id, days):
    row = _row(session, task_id)
    row.eligible_since = _now() - timedelta(days=days)
    session.add(row)
    session.commit()


def _two_priorities(client):
    low = client.post("/api/projects/", json={"name": "Low", "priority": 4}).json()["id"]
    high = client.post("/api/projects/", json={"name": "High", "priority": 2}).json()["id"]
    gpu = _gpu(client, "GPU")
    old = _task(client, low, gpu, duration=1)
    new = _task(client, high, gpu, duration=1)
    return old, new


def test_a_long_wait_lifts_a_low_priority_task(client, session):
    old, new = _two_priorities(client)
    client.post("/api/schedule/reflow")
    assert _get(client, new)["start_date"] < _get(client, old)["start_date"]
    _age(session, old, 22)  # three levels: Low (4) plays as Critical (1)
    client.post("/api/schedule/reflow")
    assert _get(client, old)["start_date"] < _get(client, new)["start_date"]


def test_an_aged_task_does_not_outrank_work_born_at_its_new_level(client, session):
    gpu = _gpu(client, "GPU")
    low = client.post("/api/projects/", json={"name": "Low", "priority": 4}).json()["id"]
    crit = client.post("/api/projects/", json={"name": "Critical", "priority": 1}).json()["id"]
    old = _task(client, low, gpu, duration=1)
    urgent = _task(client, crit, gpu, duration=1)
    _age(session, old, 10 ** 4)  # lifted to Critical, no further
    client.post("/api/schedule/reflow")
    assert _get(client, urgent)["start_date"] < _get(client, old)["start_date"]


def test_age_priority_is_capped_and_can_be_disabled(client, session):
    old, new = _two_priorities(client)
    _age(session, old, 10 ** 4)
    client.patch("/api/settings/", json={"age_priority_days": 0})
    client.post("/api/schedule/reflow")
    assert _get(client, new)["start_date"] < _get(client, old)["start_date"]


def test_age_priority_defaults_to_a_week(client):
    assert client.get("/api/settings/").json()["age_priority_days"] == 7


@pytest.mark.parametrize("bad", [-1, 366])
def test_age_priority_bounds(client, bad):
    assert client.patch("/api/settings/", json={"age_priority_days": bad}).status_code == 422


# ── Task numbers in emails (task 1106) ───────────────────────────────────────

def test_emails_carry_task_numbers(client, project_id, monkeypatch):
    import app.routers.tasks as tasks_router
    sent = []

    def capture(_session, subject, body, **_):
        sent.append((subject, body))

    monkeypatch.setattr(reflow, "send_notification", capture)
    monkeypatch.setattr(tasks_router, "send_notification", capture)
    gpu = _gpu(client, "GPU")

    done = _task(client, project_id, gpu)
    client.patch(f"/api/tasks/{done}", json={"status": "done"})
    subject, body = sent[-1]
    assert subject == f"DONE: #{done} t" and f"Task: #{done} t" in body

    up = _task(client, project_id, gpu)
    down = _task(client, project_id, gpu, depends_on_id=up)
    client.delete(f"/api/tasks/{up}")
    subject, body = sent[-1]
    assert f"#{up} 't' was deleted" in body and f"• #{down} t" in body

    far = {"start_date": date.today().isoformat(),
           "end_date": (date.today() + timedelta(days=400)).isoformat()}
    client.post(f"/api/resources/{gpu}/blockouts", json=far)
    stuck = _task(client, project_id, gpu)
    subject, body = sent[-1]
    assert subject == "Tasks could not be scheduled" and f"• #{stuck} t:" in body
