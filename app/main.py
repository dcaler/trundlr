import math
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"
_APP_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "dev"
_STARTED_AT = datetime.now(timezone.utc).strftime("%H:%M UTC")

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.database import apply_migrations, create_db_and_tables, init_engine
from app.routers import projects, resources, schedule, settings, tasks
from app.routers import caldav, runner

STATIC_DIR = Path(__file__).parent / "static"


def _strip_non_finite(value):
    """Replace inf/nan floats with their string form, recursively.

    A validation error echoes the offending input back to the client. When that
    input is a non-finite float (Infinity/NaN, which Python's JSON parser
    accepts), the default response serializer raises 'Out of range float values
    are not JSON compliant' and turns a 422 into a 500. Stringifying keeps the
    error body informative and JSON-serializable.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _strip_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strip_non_finite(v) for v in value]
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    database_url = os.getenv("DATABASE_URL", "sqlite:///trundlr.db")
    engine = init_engine(database_url)
    create_db_and_tables(engine)
    apply_migrations(engine)
    yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": _strip_non_finite(jsonable_encoder(exc.errors()))},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


_INDEX_HTML = STATIC_DIR / "index.html"

# Append ?v=<version> to local static .css/.js references so browsers fetch
# fresh assets after every deploy instead of serving a stale cached copy.
_ASSET_REF_RE = re.compile(r'(href|src)="(/static/[^"]+\.(?:css|js))"')


def _versioned_index() -> str:
    html = _INDEX_HTML.read_text()
    return _ASSET_REF_RE.sub(
        lambda m: f'{m.group(1)}="{m.group(2)}?v={_APP_VERSION}"', html
    )


@app.get("/", include_in_schema=False)
def read_root():
    return HTMLResponse(_versioned_index())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/version")
def version():
    return {"version": f"{_APP_VERSION} · {_STARTED_AT}"}


app.include_router(projects.router)
app.include_router(resources.router)
app.include_router(tasks.router)
app.include_router(schedule.router)
app.include_router(settings.router)
app.include_router(caldav.router)
app.include_router(runner.router)


@app.get("/runner.py", include_in_schema=False)
def download_runner():
    return FileResponse(
        Path(__file__).parent.parent / "runner.py",
        media_type="text/x-python",
        filename="runner.py",
    )


@app.get("/.well-known/caldav", include_in_schema=False)
def well_known_caldav_get():
    return RedirectResponse("/caldav/principal/", status_code=301)


@app.api_route("/.well-known/caldav", methods=["PROPFIND"], include_in_schema=False)
async def well_known_caldav_propfind(request: Request):
    from app.routers.caldav import (
        _d, _cal, _href_child, _resourcetype_collection, _filter_props,
        _multistatus, _requested_props,
    )
    body = await request.body()
    requested = _requested_props(body)
    all_props = {
        _d("resourcetype"):           _resourcetype_collection(),
        _d("current-user-principal"): _href_child("/caldav/principal/"),
        _cal("calendar-home-set"):    _href_child("/caldav/calendars/"),
    }
    found, missing = _filter_props(all_props, requested)
    return _multistatus([("/.well-known/caldav", found, missing)])
