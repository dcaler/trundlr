"""Shared input-validation constraints for the API surface.

These bounds turn would-be 500s into clean 4xx responses:

* Integer ids are passed to SQLite, which only stores signed 64-bit values;
  anything outside that range raises ``OverflowError`` at the driver layer
  (a 500) rather than a 404/422. Bounding ids to ``[1, MAX_DB_INT]`` rejects
  out-of-range ids before they ever reach the database.
* A schedule/utilization query window is materialised one row per day by the
  capacity engine, so an unbounded range (e.g. year 1 .. year 9999) would
  allocate millions of rows. ``MAX_RANGE_DAYS`` caps a single window.

``DBId``/``OptionalDBIdQuery`` are factories (not ``Annotated`` aliases): this
FastAPI version mishandles ``Annotated[int, Path(...)]`` under Pydantic v2, so
parameters must use the ``= Path(...)`` default style, and each parameter needs
its own field-info instance.
"""

from typing import Any

from fastapi import Path, Query

# SQLite stores signed 64-bit integers.
MAX_DB_INT = 2**63 - 1

# Upper bound on a single schedule/utilization window (inclusive day count).
# ~10 years is far beyond any realistic UI view while keeping memory bounded.
MAX_RANGE_DAYS = 3660


def DBId() -> Any:
    """Path-parameter constraint for a primary-key id (positive, in DB range)."""
    return Path(ge=1, le=MAX_DB_INT)


def OptionalDBIdQuery() -> Any:
    """Optional query-parameter id filter (e.g. ``?project_id=``)."""
    return Query(default=None, ge=1, le=MAX_DB_INT)
