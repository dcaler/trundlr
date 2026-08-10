"""Per-resource CalDAV "block" calendar: painting events in the {name}-block
calendar creates ResourceCalBlocks that reduce availability (re-flow, heatmap,
conflicts) and shade red — without showing up in the manual Blockouts list.
"""
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.database import get_db
from app.main import app
from app.models import ResourceCalBlock

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"

HUMAN = {"name": "Cale", "kind": "human", "available_from": "09:00",
         "available_to": "17:00", "available_days": 31}  # Mon–Fri, 8h


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


def _d(t):
    return f"{{{DAV}}}{t}"


def _block_ical(uid, dtstart, dtend, summary="Lunch"):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def _sync_collection_body(token=""):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:sync-collection xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        f'<d:sync-token>{token}</d:sync-token>'
        '<d:sync-level>1</d:sync-level>'
        '<d:prop><d:getetag/><cal:calendar-data/></d:prop>'
        '</d:sync-collection>'
    )


def _responses(root):
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


# ── Discovery ─────────────────────────────────────────────────────────────────

def test_block_calendar_enumerated_in_home(client):
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    resp = client.request(
        "PROPFIND", "/caldav/calendars/", headers={"Depth": "1"},
        content='<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/></d:prop></d:propfind>',
    )
    assert resp.status_code == 207
    rows = _responses(ET.fromstring(resp.text))
    hrefs = {href: props.get(_d("displayname")) for href, _, props in rows}
    assert hrefs.get(f"/caldav/calendars/{rid}/") == "Cale"
    assert hrefs.get(f"/caldav/calendars/block-{rid}/") == "Cale-block"


# ── Round-trip: PUT → REPORT/GET → DELETE ──────────────────────────────────────

def test_put_creates_block_and_roundtrips(client, session):
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    href = f"/caldav/calendars/block-{rid}/abc.ics"

    put = client.put(
        href,
        content=_block_ical("abc", "20260615T090000Z", "20260615T130000Z"),
        headers={"Content-Type": "text/calendar"},
    )
    assert put.status_code == 201
    # No rename: the block is keyed by the client's resource name ("abc").
    assert "Location" not in put.headers
    blocks = session.exec(select(ResourceCalBlock)).all()
    assert len(blocks) == 1 and blocks[0].resource_id == rid and blocks[0].uid == "abc"

    # GET it back at the same href the client created it at.
    got = client.get(href)
    assert got.status_code == 200
    assert "Lunch" in got.text
    assert "UID:abc" in got.text

    # DELETE removes it.
    assert client.delete(href).status_code == 204
    assert session.exec(select(ResourceCalBlock)).all() == []


def test_block_served_at_client_href_no_duplicate(client):
    """Regression: a block must appear in REPORT under the SAME href the client
    PUT it to, otherwise Apple Calendar ends up with a duplicate."""
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    href = f"/caldav/calendars/block-{rid}/CLIENT-GUID-123.ics"
    client.put(
        href,
        content=_block_ical("CLIENT-GUID-123", "20260615T090000Z", "20260615T100000Z"),
        headers={"Content-Type": "text/calendar"},
    )

    report = client.request(
        "REPORT", f"/caldav/calendars/block-{rid}/",
        headers={"Depth": "1", "Content-Type": "application/xml"},
        content=_sync_collection_body(),
    )
    assert report.status_code == 207
    hrefs = [
        h for h, status, _ in _responses(ET.fromstring(report.text))
        if h and h.endswith(".ics")
    ]
    assert hrefs == [href]  # exactly one, at the client's own href


# ── Read endpoint + isolation from manual blockouts ────────────────────────────

def test_calblocks_endpoint_and_not_in_blockouts(client):
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]
    client.put(
        f"/caldav/calendars/block-{rid}/x.ics",
        content=_block_ical("u1", "20260615T090000Z", "20260615T130000Z"),
        headers={"Content-Type": "text/calendar"},
    )

    segs = client.get(f"/api/resources/{rid}/calblocks").json()
    assert len(segs) == 1
    assert segs[0]["start_date"] == "2026-06-15"
    assert segs[0]["from_time"] == "09:00"
    assert segs[0]["to_time"] == "13:00"

    # Must NOT appear under the manual blockouts list.
    assert client.get(f"/api/resources/{rid}/blockouts").json() == []


# ── Scheduling: blocks reduce capacity everywhere ──────────────────────────────

def test_block_reduces_schedule_capacity(client):
    rid = client.post("/api/resources/", json=HUMAN).json()["id"]

    before = client.get(f"/api/resources/{rid}/schedule?from=2026-06-15&to=2026-06-15").json()
    assert before[0]["capacity"] == pytest.approx(8.0)  # 09:00–17:00

    # Block 09:00–13:00 (4h) on the same day.
    client.put(
        f"/caldav/calendars/block-{rid}/x.ics",
        content=_block_ical("u1", "20260615T090000Z", "20260615T130000Z"),
        headers={"Content-Type": "text/calendar"},
    )

    after = client.get(f"/api/resources/{rid}/schedule?from=2026-06-15&to=2026-06-15").json()
    assert after[0]["capacity"] == pytest.approx(4.0)  # 8h − 4h blocked
