"""Date-to-pixel mapping helpers for the Gantt timeline view.

These are pure functions over date objects — no DB, no HTTP. The JS frontend
uses the same logic (reimplemented inline) with DAY_WIDTH = 28. Tests live in
tests/test_gantt.py.
"""

from __future__ import annotations

from datetime import date
from typing import Optional


def day_offset(range_start: date, day: date) -> int:
    """Days from range_start to day. Negative when day is before range_start."""
    return (day - range_start).days


def bar_left_px(range_start: date, task_start: date, day_width: int) -> int:
    """Left offset in pixels. Clamped to 0 for tasks that start before the range."""
    return max(0, day_offset(range_start, task_start)) * day_width


def bar_width_px(
    range_start: date,
    range_end: date,
    task_start: date,
    task_end: Optional[date],
    day_width: int,
) -> int:
    """Width in pixels for the visible portion of a task bar.

    Handles:
    - tasks that start before range_start (left-clamped)
    - tasks that extend past range_end (right-clamped)
    - open-ended tasks (task_end=None) — extend to range_end
    - tasks entirely outside the range — returns 0
    """
    from_day = max(0, day_offset(range_start, task_start))
    if task_end is None:
        to_day = day_offset(range_start, range_end)
    else:
        to_day = min(day_offset(range_start, range_end), day_offset(range_start, task_end))
    return max(0, to_day - from_day + 1) * day_width
