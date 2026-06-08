"""Tests for the Gantt date-to-pixel mapping logic (Step 4.3).

Two scopes:
  1. Unit tests for app/gantt.py — pure date math, no DB.
  2. API integration test — seed a known task and assert the schedule endpoint
     returns the per-day data that the Gantt timeline consumes to position bars.
"""

import pytest
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.gantt import bar_left_px, bar_width_px, day_offset
from app.database import get_db
from app.main import app

W = 28  # day_width used in all pixel tests (matches JS constant SCHED_DAY_WIDTH)


# ── day_offset ────────────────────────────────────────────────────────────────

def test_day_offset_same_day():
    d = date(2026, 6, 1)
    assert day_offset(d, d) == 0

def test_day_offset_forward():
    assert day_offset(date(2026, 6, 1), date(2026, 6, 8)) == 7

def test_day_offset_backward():
    assert day_offset(date(2026, 6, 8), date(2026, 6, 1)) == -7

def test_day_offset_crosses_month_boundary():
    assert day_offset(date(2026, 6, 28), date(2026, 7, 2)) == 4

def test_day_offset_crosses_year_boundary():
    assert day_offset(date(2025, 12, 30), date(2026, 1, 3)) == 4


# ── bar_left_px ───────────────────────────────────────────────────────────────

def test_bar_left_at_range_start():
    d = date(2026, 6, 1)
    assert bar_left_px(d, d, W) == 0

def test_bar_left_three_days_in():
    assert bar_left_px(date(2026, 6, 1), date(2026, 6, 4), W) == 3 * W

def test_bar_left_clamped_when_before_range():
    # task_start is before range_start → clamp to 0
    assert bar_left_px(date(2026, 6, 5), date(2026, 6, 1), W) == 0

def test_bar_left_first_day_of_range():
    assert bar_left_px(date(2026, 6, 10), date(2026, 6, 10), W) == 0


# ── bar_width_px ──────────────────────────────────────────────────────────────

def test_bar_width_single_day():
    d = date(2026, 6, 1)
    assert bar_width_px(d, date(2026, 6, 30), d, d, W) == W

def test_bar_width_five_days():
    start = date(2026, 6, 1)
    assert bar_width_px(start, date(2026, 6, 30), start, date(2026, 6, 5), W) == 5 * W

def test_bar_width_open_ended_extends_to_range_end():
    # task_end=None → bar extends to range_end
    # task starts Jun 10, range ends Jun 14 → 5 days visible (10,11,12,13,14)
    w = bar_width_px(date(2026, 6, 1), date(2026, 6, 14), date(2026, 6, 10), None, W)
    assert w == 5 * W

def test_bar_width_clamped_at_range_end():
    # task Jun 25–Jul 10, range ends Jun 30 → 6 days visible (25–30)
    w = bar_width_px(date(2026, 6, 1), date(2026, 6, 30), date(2026, 6, 25), date(2026, 7, 10), W)
    assert w == 6 * W

def test_bar_width_task_starts_before_range():
    # task Jun 1–Jun 7, range starts Jun 5 → 3 days visible (5,6,7)
    w = bar_width_px(date(2026, 6, 5), date(2026, 6, 30), date(2026, 6, 1), date(2026, 6, 7), W)
    assert w == 3 * W

def test_bar_width_entirely_before_range_is_zero():
    # task ends before range starts
    w = bar_width_px(date(2026, 6, 5), date(2026, 6, 30), date(2026, 6, 1), date(2026, 6, 4), W)
    assert w == 0

def test_bar_width_entirely_after_range_is_zero():
    # task starts after range ends
    w = bar_width_px(date(2026, 6, 1), date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 5), W)
    assert w == 0

def test_bar_width_spans_entire_range():
    rng = (date(2026, 6, 1), date(2026, 6, 30))
    # task wider than range → all 30 days
    w = bar_width_px(rng[0], rng[1], date(2026, 5, 1), date(2026, 7, 31), W)
    assert w == 30 * W


# ── API integration: seed a task and verify schedule data ─────────────────────

@pytest.fixture
def api_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_schedule_endpoint_matches_seeded_task(api_client):
    """Seed a known task and assert the schedule endpoint returns per-day data
    that correctly reflects the task's committed load — the data the Gantt
    reads to verify bar position and height."""
    project = api_client.post("/api/projects/", json={"name": "P"}).json()
    resource = api_client.post("/api/resources/", json={
        "name": "Alice", "kind": "human",
        "available_from": "09:00", "available_to": "17:00", "available_days": 31,
    }).json()
    api_client.post("/api/tasks/", json={
        "title": "Design",
        "project_id": project["id"],
        "resource_ids": [resource["id"]],
        "start_date": "2026-06-03",
        "end_date": "2026-06-05",
        "load": 4.0,
    })

    resp = api_client.get(
        f"/api/resources/{resource['id']}/schedule",
        params={"from": "2026-06-01", "to": "2026-06-07"},
    )
    assert resp.status_code == 200
    days = {d["day"]: d for d in resp.json()}

    # Days outside the task window → 0 committed, bar would have zero width
    assert days["2026-06-01"]["committed"] == pytest.approx(0.0)
    assert days["2026-06-02"]["committed"] == pytest.approx(0.0)

    # Days inside the task window → 4.0 committed, bar present
    assert days["2026-06-03"]["committed"] == pytest.approx(4.0)
    assert days["2026-06-04"]["committed"] == pytest.approx(4.0)
    assert days["2026-06-05"]["committed"] == pytest.approx(4.0)

    # Also assert pixel positions are correct for this seed
    range_start = date(2026, 6, 1)
    task_start  = date(2026, 6, 3)
    task_end    = date(2026, 6, 5)

    assert bar_left_px(range_start, task_start, W) == 2 * W   # 2 days from left
    assert bar_width_px(range_start, date(2026, 6, 7), task_start, task_end, W) == 3 * W
