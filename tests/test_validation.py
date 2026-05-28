"""Step 6.3 — input-validation & error-handling audit.

Parametrized fuzzing of the API surface: malformed payloads, extreme date
ranges, out-of-range ids, and non-finite numbers. The contract under test is
the one the plan calls for: **no 5xx responses, only sane 4xx**.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_db
from app.main import app
from app.validation import MAX_DB_INT, MAX_RANGE_DAYS


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
    # raise_server_exceptions=False so a 500 surfaces as a response we can
    # assert on, rather than blowing up the test itself.
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture(name="project_id")
def project_id_fixture(client):
    return client.post("/api/projects/", json={"name": "P"}).json()["id"]


@pytest.fixture(name="resource_id")
def resource_id_fixture(client):
    return client.post(
        "/api/resources/", json={"name": "R", "kind": "gpu", "capacity": 4.0}
    ).json()["id"]


# JSON tokens the stdlib parser accepts but standard JSON does not.
INF = float("inf")
NEG_INF = float("-inf")
NAN = float("nan")

OVERSIZED_ID = MAX_DB_INT + 1
NEGATIVE_ID = -5


# --------------------------------------------------------------------------
# Master invariant: nothing in the matrix below may ever return a 5xx.
# --------------------------------------------------------------------------

def _fuzz_requests(project_id, resource_id):
    """A broad matrix of hostile requests as (method, url, json_body) tuples."""
    return [
        # --- oversized / out-of-range path ids (would OverflowError in SQLite) ---
        ("GET", f"/api/projects/{OVERSIZED_ID}", None),
        ("GET", f"/api/resources/{OVERSIZED_ID}", None),
        ("GET", f"/api/tasks/{OVERSIZED_ID}", None),
        ("PATCH", f"/api/projects/{OVERSIZED_ID}", {"name": "x"}),
        ("PATCH", f"/api/resources/{OVERSIZED_ID}", {"name": "x"}),
        ("PATCH", f"/api/tasks/{OVERSIZED_ID}", {"title": "x"}),
        ("DELETE", f"/api/projects/{OVERSIZED_ID}", None),
        ("DELETE", f"/api/resources/{OVERSIZED_ID}", None),
        ("DELETE", f"/api/tasks/{OVERSIZED_ID}", None),
        ("GET", f"/api/resources/{OVERSIZED_ID}/schedule?from=2026-01-01&to=2026-01-02", None),
        ("GET", f"/api/resources/{OVERSIZED_ID}/conflicts?from=2026-01-01&to=2026-01-02", None),
        ("GET", f"/api/tasks/?project_id={OVERSIZED_ID}", None),
        # --- negative / zero ids ---
        ("GET", f"/api/tasks/{NEGATIVE_ID}", None),
        ("GET", "/api/resources/0", None),
        ("GET", f"/api/tasks/?project_id={NEGATIVE_ID}", None),
        # --- non-finite numbers in bodies ---
        ("POST", "/api/resources/", {"name": "x", "kind": "cpu", "capacity": INF}),
        ("POST", "/api/resources/", {"name": "x", "kind": "cpu", "capacity": NAN}),
        ("POST", "/api/resources/", {"name": "x", "kind": "cpu", "capacity": NEG_INF}),
        ("PATCH", f"/api/resources/{resource_id}", {"capacity": INF}),
        ("PATCH", f"/api/resources/{resource_id}", {"capacity": NAN}),
        ("POST", "/api/tasks/", {"title": "x", "project_id": project_id, "load": INF}),
        ("POST", "/api/tasks/", {"title": "x", "project_id": project_id, "load": NAN}),
        # --- extreme / malformed date ranges ---
        (
            "GET",
            f"/api/resources/{resource_id}/schedule?from=0001-01-01&to=9999-12-31",
            None,
        ),
        ("GET", "/api/utilization?from=0001-01-01&to=9999-12-31", None),
        (
            "GET",
            f"/api/resources/{resource_id}/conflicts?from=0001-01-01&to=9999-12-31",
            None,
        ),
        (
            "GET",
            f"/api/resources/{resource_id}/schedule?from=2026-06-01&to=2026-05-01",
            None,
        ),
        ("GET", f"/api/resources/{resource_id}/schedule?from=garbage&to=2026-05-01", None),
        ("GET", f"/api/resources/{resource_id}/schedule?from=2026-13-45&to=2026-05-01", None),
        ("GET", f"/api/resources/{resource_id}/schedule", None),  # missing required params
        # --- empty / wrong-typed bodies ---
        ("POST", "/api/projects/", {"name": ""}),
        ("POST", "/api/projects/", {}),
        ("POST", "/api/resources/", {"name": "x", "kind": "wizard", "capacity": 1}),
        ("POST", "/api/resources/", {"name": "x", "kind": "cpu", "capacity": "lots"}),
        ("POST", "/api/resources/", {"name": "x", "kind": "cpu", "capacity": -1}),
        ("POST", "/api/resources/", {"name": "x", "kind": "cpu", "capacity": 0}),
        ("POST", "/api/tasks/", {"title": "x", "project_id": "not-an-int"}),
        ("POST", "/api/tasks/", {"title": "x", "project_id": project_id, "status": "??"}),
        (
            "POST",
            "/api/tasks/",
            {
                "title": "x",
                "project_id": project_id,
                "start_date": "2026-06-30",
                "end_date": "2026-06-01",
            },
        ),
    ]


def test_no_request_in_fuzz_matrix_returns_5xx(client, project_id, resource_id):
    failures = []
    for method, url, body in _fuzz_requests(project_id, resource_id):
        resp = client.request(method, url, json=body)
        # Body must always be valid JSON (a non-finite float would 500 here).
        try:
            resp.json()
        except json.JSONDecodeError:
            failures.append((method, url, "non-JSON body", resp.status_code))
        if resp.status_code >= 500:
            failures.append((method, url, body, resp.status_code))
    assert not failures, f"requests returned 5xx / bad body: {failures}"


# --------------------------------------------------------------------------
# Out-of-range ids -> 422 (not 500, not 404).
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        f"/api/projects/{OVERSIZED_ID}",
        f"/api/resources/{OVERSIZED_ID}",
        f"/api/tasks/{OVERSIZED_ID}",
        "/api/tasks/-1",
        "/api/tasks/0",
        f"/api/resources/{OVERSIZED_ID}/schedule?from=2026-01-01&to=2026-01-02",
    ],
)
def test_out_of_range_path_id_returns_422(client, url):
    assert client.get(url).status_code == 422


def test_oversized_project_id_query_filter_returns_422(client):
    assert client.get(f"/api/tasks/?project_id={OVERSIZED_ID}").status_code == 422


def test_max_valid_id_is_in_range_so_404_not_422(client):
    # MAX_DB_INT is a valid (if absent) id: it must 404, proving the bound is
    # inclusive and the 422 is reserved for genuinely out-of-range values.
    assert client.get(f"/api/tasks/{MAX_DB_INT}").status_code == 404


def test_oversized_body_project_id_returns_422(client):
    resp = client.post("/api/tasks/", json={"title": "x", "project_id": OVERSIZED_ID})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Non-finite numbers -> 422 with a serializable body.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [INF, NEG_INF, NAN])
def test_non_finite_capacity_rejected(client, bad):
    resp = client.post(
        "/api/resources/", json={"name": "x", "kind": "cpu", "capacity": bad}
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()  # body parsed cleanly -> no serialization 500


@pytest.mark.parametrize("bad", [INF, NEG_INF, NAN])
def test_non_finite_load_rejected(client, project_id, bad):
    resp = client.post(
        "/api/tasks/", json={"title": "x", "project_id": project_id, "load": bad}
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()


def test_non_finite_capacity_patch_rejected(client, resource_id):
    resp = client.patch(f"/api/resources/{resource_id}", json={"capacity": NAN})
    assert resp.status_code == 422
    assert "detail" in resp.json()


# --------------------------------------------------------------------------
# Date-range span cap (both sides of the MAX_RANGE_DAYS boundary).
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/api/resources/{rid}/schedule",
        "/api/resources/{rid}/conflicts",
    ],
)
def test_overlong_range_rejected(client, resource_id, path):
    # MAX_RANGE_DAYS + 1 inclusive days: from + MAX_RANGE_DAYS day offset.
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    end = start + timedelta(days=MAX_RANGE_DAYS)  # inclusive count = MAX + 1
    url = path.format(rid=resource_id) + f"?from={start}&to={end}"
    assert client.get(url).status_code == 422


def test_overlong_utilization_range_rejected(client):
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    end = start + timedelta(days=MAX_RANGE_DAYS)
    assert client.get(f"/api/utilization?from={start}&to={end}").status_code == 422


def test_range_exactly_at_cap_is_allowed(client, resource_id):
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    end = start + timedelta(days=MAX_RANGE_DAYS - 1)  # inclusive count = MAX
    resp = client.get(
        f"/api/resources/{resource_id}/schedule?from={start}&to={end}"
    )
    assert resp.status_code == 200
    assert len(resp.json()) == MAX_RANGE_DAYS


def test_inverted_range_still_rejected(client, resource_id):
    resp = client.get(
        f"/api/resources/{resource_id}/schedule?from=2026-06-01&to=2026-05-01"
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Malformed dates and empty strings.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad_date", ["garbage", "2026-13-45", "06/01/2026", "2026-02-30"])
def test_malformed_query_date_returns_422(client, resource_id, bad_date):
    resp = client.get(
        f"/api/resources/{resource_id}/schedule?from={bad_date}&to=2026-12-31"
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {},  # missing required name
    ],
)
def test_empty_or_missing_project_name_returns_422(client, payload):
    assert client.post("/api/projects/", json=payload).status_code == 422


def test_empty_task_title_returns_422(client, project_id):
    resp = client.post("/api/tasks/", json={"title": "", "project_id": project_id})
    assert resp.status_code == 422


def test_malformed_task_date_in_body_returns_422(client, project_id):
    resp = client.post(
        "/api/tasks/",
        json={"title": "x", "project_id": project_id, "start_date": "2026-99-99"},
    )
    assert resp.status_code == 422
