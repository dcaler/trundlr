import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.database import apply_migrations, create_db_and_tables, init_engine
from app.routers import projects, resources, schedule, tasks

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


@app.get("/", include_in_schema=False)
def read_root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(projects.router)
app.include_router(resources.router)
app.include_router(tasks.router)
app.include_router(schedule.router)
