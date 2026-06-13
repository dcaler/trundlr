"""CalDAV (WebDAV protocol) router tests — distinct from the legacy read-only
/api/resources/{id}/calendar.ics feed covered by test_ical.py.

Locks in three fixes:
  1. Unscheduled tasks never appear (no bogus all-day events).
  2. Deleting / unscheduling a task is reported to the client as a 404
     sync-collection member so it stops being orphaned.
  3. The etag includes the project name, so a project rename invalidates it.
"""
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"

HUMAN = {"name": "Alice", "kind": "human", "available_from": "09:00",
         "available_to": "17:00", "available_days": 31}


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


# ── helpers ──────────────────────────────────────────────────────────────────

def _d(t):
    return f"{{{DAV}}}{t}"


def _cal(t):
    return f"{{{CALDAV}}}{t}"


def _sync_collection_body(token=""):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:sync-collection xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        f'<d:sync-token>{token}</d:sync-token>'
        '<d:sync-level>1</d:sync-level>'
        '<d:prop><d:getetag/><cal:calendar-data/></d:prop>'
        '</d:sync-collection>'
    )


def _multiget_body(rid, *task_ids):
    hrefs = "".join(
        f"<d:href>/caldav/calendars/{rid}/task-{tid}%40trundlr.ics</d:href>"
        for tid in task_ids
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<cal:calendar-multiget xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        '<d:prop><d:getetag/><cal:calendar-data/></d:prop>'
        f'{hrefs}'
        '</cal:calendar-multiget>'
    )


def _report(client, rid, body):
    resp = client.request(
        "REPORT", f"/caldav/calendars/{rid}/",
        content=body, headers={"Depth": "1", "Content-Type": "application/xml"},
    )
    assert resp.status_code == 207, resp.text
    return ET.fromstring(resp.text)


def _responses(root):
    """Return list of (href, status_text_or_None, {tag: text}) per d:response."""
    out = []
    for resp in root.findall(_d("response")):
        href = resp.findtext(_d("href"))
        top_status = resp.findtext(_d("status"))
        props = {}
        for ps in resp.findall(_d("propstat")):
            for prop in ps.findall(_d("prop")):
                for child in prop:
                    props[child.tag] = child.text
        out.append((href, top_status, props))
    return out


def _make_scheduled_task(client, project_id, resource_id, title="Scheduled"):
    return client.post("/api/tasks/", json={
        "title": title,
        "project_id": project_id,
        "resource_ids": [resource_id],
        "start_date": "2026-06-01T09:00:00",
        "end_date": "2026-06-01T11:00:00",
    }).json()


# ── Bug 1: no all-day events for unscheduled tasks ────────────────────────────

def test_unscheduled_task_excluded_from_report(client):
    project = client.post("/api/projects/", json={"name": "P"}).json()
    resource = client.post("/api/resources/", json=HUMAN).json()
    rid = resource["id"]
    scheduled = _make_scheduled_task(client, project["id"], rid, "Has Time")
    client.post("/api/tasks/", json={
        "title": "No Time",
        "project_id": project["id"],
        "resource_ids": [rid],
    })

    root = _report(client, rid, _sync_collection_body())
    cal_data = [
        props.get(_cal("calendar-data"))
        for _, status, props in _responses(root)
        if status is None and _cal("calendar-data") in props
    ]
    assert len(cal_data) == 1
    body = cal_data[0]
    assert "Has Time" in body
    assert "No Time" not in body
    # A timed event, never an all-day DATE value.
    assert "VALUE=DATE" not in body
    assert "DTSTART;VALUE=DATE:" not in body
    assert f"task-{scheduled['id']}@trundlr" in body


def test_unscheduled_task_excluded_from_propfind(client):
    project = client.post("/api/projects/", json={"name": "P"}).json()
    resource = client.post("/api/resources/", json=HUMAN).json()
    rid = resource["id"]
    _make_scheduled_task(client, project["id"], rid, "Has Time")
    client.post("/api/tasks/", json={
        "title": "No Time", "project_id": project["id"], "resource_ids": [rid],
    })

    resp = client.request(
        "PROPFIND", f"/caldav/calendars/{rid}/",
        headers={"Depth": "1"},
        content='<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>',
    )
    assert resp.status_code == 207
    member_hrefs = [
        href for href, _, _ in _responses(ET.fromstring(resp.text))
        if href and href.endswith(".ics")
    ]
    assert len(member_hrefs) == 1


# ── Bug 2: deletions don't orphan — token forces a full re-sync ───────────────

def test_presenting_a_token_forces_full_resync(client):
    """A sync-collection with any prior token gets DAV:valid-sync-token (403),
    forcing the client to re-enumerate from scratch — which flushes orphans."""
    project = client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    task = _make_scheduled_task(client, project["id"], rid)

    # Initial (token-less) sync returns the event and a fresh token.
    root = _report(client, rid, _sync_collection_body())
    token = root.findtext(_d("sync-token"))
    assert token
    hrefs = [h for h, _, _ in _responses(root) if h and h.endswith(".ics")]
    assert hrefs == [f"/caldav/calendars/{rid}/task-{task['id']}@trundlr.ics"]

    # Presenting that token is rejected → client must restart with empty token.
    rejected = client.request(
        "REPORT", f"/caldav/calendars/{rid}/",
        content=_sync_collection_body(token),
        headers={"Depth": "1", "Content-Type": "application/xml"},
    )
    assert rejected.status_code == 403
    assert "valid-sync-token" in rejected.text


def test_deleted_task_absent_from_full_resync(client):
    project = client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    task = _make_scheduled_task(client, project["id"], rid)

    assert client.delete(f"/api/tasks/{task['id']}").status_code in (200, 204)

    # Token-less (initial) sync = the full truth; the deleted task is simply gone.
    root = _report(client, rid, _sync_collection_body())
    hrefs = [h for h, _, _ in _responses(root) if h and h.endswith(".ics")]
    assert hrefs == []


def test_multiget_404s_missing_event(client):
    project = client.post("/api/projects/", json={"name": "P"}).json()
    resource = client.post("/api/resources/", json=HUMAN).json()
    rid = resource["id"]
    task = _make_scheduled_task(client, project["id"], rid)
    client.delete(f"/api/tasks/{task['id']}")

    root = _report(client, rid, _multiget_body(rid, task["id"]))
    statuses = [status for _, status, _ in _responses(root)]
    assert any(s and "404" in s for s in statuses)


# ── Bug 3: etag tracks the project name ───────────────────────────────────────

def test_etag_changes_when_project_renamed(client):
    project = client.post("/api/projects/", json={"name": "Old"}).json()
    resource = client.post("/api/resources/", json=HUMAN).json()
    rid = resource["id"]
    _make_scheduled_task(client, project["id"], rid)

    def current_etag():
        root = _report(client, rid, _sync_collection_body())
        for _, status, props in _responses(root):
            if status is None and _d("getetag") in props:
                return props[_d("getetag")]
        raise AssertionError("no etag in report")

    before = current_etag()
    assert client.patch(f"/api/projects/{project['id']}", json={"name": "Renamed"}).status_code == 200
    after = current_etag()
    assert before != after
