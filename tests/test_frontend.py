"""Smoke tests for the web UI shell (Step 4.1).

Checks that the app serves the SPA index and all static assets that the
page depends on, with correct content types.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_returns_html():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_root_body_contains_nav_links():
    resp = client.get("/")
    body = resp.text
    assert "#/projects" in body
    assert "#/resources" in body
    assert "#/schedule" in body


def test_root_body_contains_app_element():
    resp = client.get("/")
    assert 'id="app"' in resp.text


def test_static_css_loads():
    resp = client.get("/static/css/style.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


def test_static_api_js_loads():
    resp = client.get("/static/js/api.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_static_app_js_loads():
    resp = client.get("/static/js/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_static_projects_view_loads():
    resp = client.get("/static/js/views/projects.js")
    assert resp.status_code == 200


def test_static_resources_view_loads():
    resp = client.get("/static/js/views/resources.js")
    assert resp.status_code == 200


def test_static_schedule_view_loads():
    resp = client.get("/static/js/views/schedule.js")
    assert resp.status_code == 200


def test_missing_static_asset_404():
    resp = client.get("/static/js/nonexistent.js")
    assert resp.status_code == 404


def test_api_still_reachable():
    """Mounting static files must not shadow /api routes."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
