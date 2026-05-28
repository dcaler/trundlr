from typing import Generator, Optional

from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Project, Resource, Task

_engine = None


def get_engine(database_url: str = "sqlite:///trundlr.db"):
    """Create and configure the SQLAlchemy engine.

    For SQLite, enables foreign key constraint enforcement via PRAGMA.
    """
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_db_and_tables(engine):
    """Create all tables in the database."""
    SQLModel.metadata.create_all(engine)


def apply_migrations(engine):
    """Run additive schema migrations for columns added after initial creation."""
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA table_info(project)"))
        project_cols = {row[1] for row in result}
        if "folder" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN folder TEXT"))
            conn.commit()

        result = conn.execute(text("PRAGMA table_info(task)"))
        task_cols = {row[1] for row in result}
        if "duration" not in task_cols:
            conn.execute(text("ALTER TABLE task ADD COLUMN duration REAL"))
            conn.commit()

        # Promote date-only strings to full datetime strings so SQLAlchemy's
        # DateTime processor can parse them after the start_date/end_date type
        # was changed from date → datetime.
        for col in ("start_date", "end_date"):
            conn.execute(text(
                f"UPDATE task SET {col} = {col} || ' 00:00:00' "
                f"WHERE {col} IS NOT NULL AND length({col}) = 10"
            ))
        conn.commit()


def get_session(engine) -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session.

    Usage in routes:
        @app.get("/items")
        def read_items(session: Session = Depends(get_session)):
            ...
    """
    with Session(engine) as session:
        yield session


def init_engine(database_url: str = "sqlite:///trundlr.db"):
    """Initialize the module-level engine used by get_db. Called once on startup."""
    global _engine
    _engine = get_engine(database_url)
    return _engine


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for route handlers; uses the module-level engine."""
    with Session(_engine) as session:
        yield session
