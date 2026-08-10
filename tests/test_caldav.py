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
        "title": "No Time", "project_id": project["id"],
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


def test_caldav_delete_of_task_with_dependent_does_not_500(client):
    # Regression: a raw `DELETE FROM task` hit the depends_on_id self-FK and
    # raised sqlite3.IntegrityError (500) when another task depended on the
    # one being deleted via CalDAV. The REST /api/tasks/{id} delete already
    # clears dependents first; caldav_delete_event must do the same.
    project = client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    predecessor = _make_scheduled_task(client, project["id"], rid, title="Pred")
    dependent = client.post("/api/tasks/", json={
        "title": "Dep",
        "project_id": project["id"],
        "resource_ids": [rid],
        "depends_on_id": predecessor["id"],
    }).json()

    resp = client.delete(f"/caldav/calendars/{rid}/task-{predecessor['id']}@trundlr.ics")
    assert resp.status_code == 204, resp.text

    refreshed = client.get(f"/api/tasks/{dependent['id']}").json()
    assert refreshed["depends_on_id"] is None
    assert refreshed["dependency_broken"] is True


def _event_ical(uid, dtstart, dtend, summary="Resized"):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def test_caldav_put_resizing_event_updates_duration(client):
    # Regression: dragging/resizing a task's event in a calendar client (e.g.
    # marking it done at a different actual time than originally scheduled)
    # updated start/end via PUT but left the stale `duration` field behind,
    # so it no longer matched end - start.
    project = client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    task = client.post("/api/tasks/", json={
        "title": "T", "project_id": project["id"], "resource_ids": [rid],
        "start_date": "2026-07-23T10:20:00", "end_date": "2026-07-23T12:20:00",
        "duration": 2.0,
    }).json()

    uid = f"task-{task['id']}@trundlr"
    body = _event_ical(uid, "20260723T102000", "20260723T112400")
    resp = client.put(f"/caldav/calendars/{rid}/task-{task['id']}@trundlr.ics", content=body)
    assert resp.status_code == 204, resp.text

    refreshed = client.get(f"/api/tasks/{task['id']}").json()
    assert refreshed["start_date"] == "2026-07-23T10:20:00"
    assert refreshed["end_date"] == "2026-07-23T11:24:00"
    assert refreshed["duration"] == pytest.approx(1.07)


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


# ── Calendar-authored tasks: duration, pinning, all-day clamping ──────────────
# A client that creates or drags an event has deliberately chosen a time. Such
# tasks are pinned so re-flow preserves the slot (and routes other work around
# it) rather than sweeping them to the earliest opening.

def _allday_ical(uid, dtstart, dtend, summary="All day"):
    """An all-day VEVENT: DATE values, DTEND exclusive."""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART;VALUE=DATE:{dtstart}\r\n"
        f"DTEND;VALUE=DATE:{dtend}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def _put_new(client, rid, body, name="4F2A1C9E-0B7D-4E11-9C3A-77AA00112233"):
    """PUT a client-authored event (random UID) and return the created task."""
    resp = client.put(f"/caldav/calendars/{rid}/{name}.ics", content=body)
    assert resp.status_code == 201, resp.text
    task_id = int(resp.headers["Location"].rsplit("task-", 1)[1].split("@", 1)[0])
    return client.get(f"/api/tasks/{task_id}").json()


def test_put_new_event_creates_pinned_task_with_duration(client):
    project = client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]

    task = _put_new(client, rid, _event_ical(
        "4F2A1C9E-0B7D-4E11-9C3A-77AA00112233",
        "20260810T140000Z", "20260810T163000Z", summary="Write the section",
    ))

    assert task["title"] == "Write the section"
    assert task["start_date"] == "2026-08-10T14:00:00"
    assert task["end_date"] == "2026-08-10T16:30:00"
    # Duration was previously left None on create — only the update path set it.
    assert task["duration"] == pytest.approx(2.5)
    assert task["pinned"] is True
    assert task["resource_ids"] == [rid]
    assert task["project_id"] == project["id"]


def test_reflow_preserves_a_calendar_authored_task(client):
    """The point of pinning: re-flow must not move the chosen slot."""
    client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    task = _put_new(client, rid, _event_ical(
        "SOME-CLIENT-UID", "20260810T140000Z", "20260810T163000Z",
    ))

    result = client.post("/api/schedule/reflow").json()
    assert result["pinned"] == 1

    after = client.get(f"/api/tasks/{task['id']}").json()
    assert after["start_date"] == task["start_date"]
    assert after["end_date"] == task["end_date"]


def test_put_allday_event_clamps_to_working_hours(client):
    """A literal all-day event is a 24h task that over-allocates the resource
    and, pinned, walls off the whole day. Clamp it to the working window."""
    client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]  # 09:00–17:00 Mon–Fri

    # 2026-08-12 is a Wednesday; DTEND is the exclusive next day.
    task = _put_new(client, rid, _allday_ical("AD-1", "20260812", "20260813"))

    assert task["start_date"] == "2026-08-12T09:00:00"
    assert task["end_date"] == "2026-08-12T17:00:00"
    assert task["duration"] == pytest.approx(8.0)
    assert task["pinned"] is True

    # 8h of work against 8h of availability is not an over-allocation.
    conflicts = client.get(
        f"/api/resources/{rid}/conflicts?from=2026-08-12&to=2026-08-12"
    ).json()
    assert conflicts == []


def test_put_multiday_allday_spans_first_to_last_working_day(client):
    client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]

    # Wed 12th through Fri 14th inclusive (DTEND = Sat 15th, exclusive).
    task = _put_new(client, rid, _allday_ical("AD-2", "20260812", "20260815"))

    assert task["start_date"] == "2026-08-12T09:00:00"
    assert task["end_date"] == "2026-08-14T17:00:00"
    # Working hours covered (3 × 8h), not the 80h of wall-clock span.
    assert task["duration"] == pytest.approx(24.0)


def test_put_allday_on_a_non_working_day_keeps_literal_span(client):
    """Nothing to clamp to when the resource never works that day, so the
    literal span stands and the resulting over-allocation stays visible."""
    client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]  # Mon–Fri

    # 2026-08-15 is a Saturday.
    task = _put_new(client, rid, _allday_ical("AD-3", "20260815", "20260816"))

    assert task["start_date"] == "2026-08-15T00:00:00"
    assert task["end_date"] == "2026-08-16T00:00:00"


def test_put_allday_skips_a_blockout_when_clamping(client):
    """Clamping follows real availability: a blockout shrinks the window."""
    client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    client.post(f"/api/resources/{rid}/blockouts", json={
        "start_date": "2026-08-12", "end_date": "2026-08-12",
        "from_time": "09:00", "to_time": "12:00", "note": "dentist",
    })

    task = _put_new(client, rid, _allday_ical("AD-4", "20260812", "20260813"))

    assert task["start_date"] == "2026-08-12T12:00:00"
    assert task["end_date"] == "2026-08-12T17:00:00"
    assert task["duration"] == pytest.approx(5.0)


def test_put_resizing_event_pins_the_task(client):
    """Dragging an existing task's event is as deliberate as creating one."""
    project = client.post("/api/projects/", json={"name": "P"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    task = client.post("/api/tasks/", json={
        "title": "T", "project_id": project["id"], "resource_ids": [rid],
        "start_date": "2026-07-23T10:20:00", "end_date": "2026-07-23T12:20:00",
    }).json()
    assert task["pinned"] is False

    uid = f"task-{task['id']}@trundlr"
    resp = client.put(
        f"/caldav/calendars/{rid}/task-{task['id']}@trundlr.ics",
        content=_event_ical(uid, "20260723T140000", "20260723T160000"),
    )
    assert resp.status_code == 204, resp.text

    refreshed = client.get(f"/api/tasks/{task['id']}").json()
    assert refreshed["pinned"] is True
    assert refreshed["start_date"] == "2026-07-23T14:00:00"


# ── "Project: Title" summaries route the task to that project ────────────────

def test_summary_prefix_selects_the_project(client):
    """The headline case: "DR+: Make new figure" lands in project DR+."""
    default = client.post("/api/projects/", json={"name": "Inbox"}).json()
    drplus = client.post("/api/projects/", json={"name": "DR+"}).json()
    client.patch("/api/settings/", json={"caldav_default_project_id": default["id"]})
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]

    task = _put_new(client, rid, _event_ical(
        "UID-DRPLUS", "20260810T140000Z", "20260810T150000Z",
        summary="DR+: Make new figure",
    ))

    assert task["title"] == "Make new figure"
    assert task["project_id"] == drplus["id"]


def test_summary_prefix_beats_the_default_project(client):
    """Routing by prefix overrides caldav_default_project_id."""
    default = client.post("/api/projects/", json={"name": "Inbox"}).json()
    other = client.post("/api/projects/", json={"name": "Grant"}).json()
    client.patch("/api/settings/", json={"caldav_default_project_id": default["id"]})
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]

    task = _put_new(client, rid, _event_ical(
        "UID-G", "20260810T140000Z", "20260810T150000Z", summary="Grant: Draft",
    ))
    assert task["project_id"] == other["id"]
    assert task["title"] == "Draft"


def test_summary_prefix_is_case_insensitive_and_space_optional(client):
    client.post("/api/projects/", json={"name": "Inbox"}).json()
    drplus = client.post("/api/projects/", json={"name": "DR+"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]

    task = _put_new(client, rid, _event_ical(
        "UID-CASE", "20260810T140000Z", "20260810T150000Z",
        summary="dr+:Make new figure",
    ))
    assert task["project_id"] == drplus["id"]
    assert task["title"] == "Make new figure"


def test_unmatched_prefix_keeps_the_whole_summary(client):
    """A colon that isn't a project name must not eat the first word."""
    inbox = client.post("/api/projects/", json={"name": "Inbox"}).json()
    client.patch("/api/settings/", json={"caldav_default_project_id": inbox["id"]})
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]

    task = _put_new(client, rid, _event_ical(
        "UID-LUNCH", "20260810T140000Z", "20260810T150000Z",
        summary="Lunch: with Bob",
    ))
    assert task["title"] == "Lunch: with Bob"
    assert task["project_id"] == inbox["id"]


def test_longest_project_name_wins(client):
    """A project whose own name contains a colon still resolves."""
    client.post("/api/projects/", json={"name": "DR"}).json()
    phase = client.post("/api/projects/", json={"name": "DR: Phase 2"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]

    task = _put_new(client, rid, _event_ical(
        "UID-LONG", "20260810T140000Z", "20260810T150000Z",
        summary="DR: Phase 2: Make new figure",
    ))
    assert task["project_id"] == phase["id"]
    assert task["title"] == "Make new figure"


def test_event_roundtrip_does_not_accumulate_the_prefix(client):
    """The server emits "{project}: {title}"; a client PUTing that back
    unedited must not end up with "DR+: DR+: ..."."""
    drplus = client.post("/api/projects/", json={"name": "DR+"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    task = _put_new(client, rid, _event_ical(
        "UID-RT", "20260810T140000Z", "20260810T150000Z",
        summary="DR+: Make new figure",
    ))

    # Fetch exactly what the server serves, then PUT it straight back.
    served = client.get(f"/caldav/calendars/{rid}/task-{task['id']}@trundlr.ics")
    assert "SUMMARY:DR+: Make new figure" in served.text
    resp = client.put(
        f"/caldav/calendars/{rid}/task-{task['id']}@trundlr.ics", content=served.text,
    )
    assert resp.status_code == 204, resp.text

    after = client.get(f"/api/tasks/{task['id']}").json()
    assert after["title"] == "Make new figure"
    assert after["project_id"] == drplus["id"]


def test_retitling_an_event_moves_the_task_between_projects(client):
    old = client.post("/api/projects/", json={"name": "Inbox"}).json()
    new = client.post("/api/projects/", json={"name": "DR+"}).json()
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    task = client.post("/api/tasks/", json={
        "title": "Make new figure", "project_id": old["id"], "resource_ids": [rid],
        "start_date": "2026-08-10T14:00:00", "end_date": "2026-08-10T15:00:00",
    }).json()

    uid = f"task-{task['id']}@trundlr"
    resp = client.put(
        f"/caldav/calendars/{rid}/task-{task['id']}@trundlr.ics",
        content=_event_ical(uid, "20260810T140000", "20260810T150000",
                            summary="DR+: Make new figure"),
    )
    assert resp.status_code == 204, resp.text

    after = client.get(f"/api/tasks/{task['id']}").json()
    assert after["project_id"] == new["id"]
    assert after["title"] == "Make new figure"
