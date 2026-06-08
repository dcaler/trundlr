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
        if "archived" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"))
            conn.commit()

        result = conn.execute(text("PRAGMA table_info(task)"))
        task_cols = {row[1] for row in result}
        if "duration" not in task_cols:
            conn.execute(text("ALTER TABLE task ADD COLUMN duration REAL"))
            conn.commit()

        # Promote date-only strings to full datetime strings so SQLAlchemy's
        # DateTime processor can parse them after the start_date/end_date type
        # was changed from date → datetime.
        if "depends_on_id" not in task_cols:
            conn.execute(text("ALTER TABLE task ADD COLUMN depends_on_id INTEGER REFERENCES task(id)"))
            conn.commit()

        if "description" not in task_cols:
            conn.execute(text("ALTER TABLE task ADD COLUMN description TEXT"))
            conn.commit()

        # Make resource.capacity nullable if the DB was created with the old schema
        # (capacity was NOT NULL). SQLite can't ALTER COLUMN, so we recreate the table.
        result = conn.execute(text("PRAGMA table_info(resource)"))
        resource_info = list(result)
        capacity_row = next((r for r in resource_info if r[1] == "capacity"), None)
        if capacity_row and capacity_row[3] == 1:  # notnull flag == 1 → NOT NULL
            existing_col_names = {r[1] for r in resource_info}
            avail_ddl = (
                ", available_from TEXT, available_to TEXT, available_days INTEGER"
                if "available_from" in existing_col_names else ""
            )
            avail_sel = (
                ", available_from, available_to, available_days"
                if "available_from" in existing_col_names else ""
            )
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text(f"""
                CREATE TABLE resource_new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR NOT NULL,
                    kind VARCHAR(5) NOT NULL,
                    capacity REAL{avail_ddl}
                )
            """))
            conn.execute(text(
                f"INSERT INTO resource_new (id, name, kind, capacity{avail_sel})"
                f" SELECT id, name, kind, capacity{avail_sel} FROM resource"
            ))
            conn.execute(text("DROP TABLE resource"))
            conn.execute(text("ALTER TABLE resource_new RENAME TO resource"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_resource_name ON resource (name)"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()

        result = conn.execute(text("PRAGMA table_info(resource)"))
        resource_cols = {row[1] for row in result}
        if "available_from" not in resource_cols:
            conn.execute(text("ALTER TABLE resource ADD COLUMN available_from TEXT"))
            conn.execute(text("ALTER TABLE resource ADD COLUMN available_to TEXT"))
            conn.execute(text("ALTER TABLE resource ADD COLUMN available_days INTEGER"))
            conn.execute(text(
                "UPDATE resource SET available_from='09:00', available_to='17:00', "
                "available_days=31 WHERE kind='human'"
            ))
            conn.execute(text("UPDATE resource SET capacity=NULL WHERE kind='human'"))
            conn.commit()

        # Backfill any resources still missing availability — covers DBs where the
        # columns exist but were never populated (e.g. cpu/gpu from earlier migrations).
        conn.execute(text(
            "UPDATE resource SET available_from='09:00', available_to='17:00', available_days=31 "
            "WHERE available_from IS NULL AND kind='human'"
        ))
        conn.execute(text(
            "UPDATE resource SET available_from='00:00', available_to='23:59', available_days=127 "
            "WHERE available_from IS NULL AND kind IN ('ai', 'cpu', 'gpu')"
        ))
        conn.commit()

        for col in ("start_date", "end_date"):
            conn.execute(text(
                f"UPDATE task SET {col} = {col} || ' 00:00:00' "
                f"WHERE {col} IS NOT NULL AND length({col}) = 10"
            ))
        conn.commit()

        # The old schema had `load FLOAT NOT NULL` on task. The model no longer
        # has this field, so SQLAlchemy's INSERTs omit it and SQLite raises
        # "NOT NULL constraint failed". Drop it via table recreation (safer than
        # ALTER TABLE DROP COLUMN which requires SQLite ≥ 3.35 and may not be
        # available in all Docker base images).
        result = conn.execute(text("PRAGMA table_info(task)"))
        task_info = list(result)
        load_col = next((r for r in task_info if r[1] == 'load'), None)
        if load_col is not None:
            existing = {r[1] for r in task_info}
            keep = [c for c in [
                'id', 'title', 'description', 'status', 'start_date', 'end_date',
                'duration', 'command', 'exit_code', 'log_tail', 'project_id', 'depends_on_id',
            ] if c in existing]
            cols = ', '.join(keep)
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text("DROP TABLE IF EXISTS task_new"))
            conn.execute(text("""
                CREATE TABLE task_new (
                    id            INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    title         VARCHAR NOT NULL,
                    description   TEXT,
                    status        VARCHAR(11) NOT NULL,
                    start_date    DATE,
                    end_date      DATE,
                    duration      REAL,
                    command       TEXT,
                    exit_code     INTEGER,
                    log_tail      TEXT,
                    project_id    INTEGER NOT NULL REFERENCES project(id),
                    depends_on_id INTEGER REFERENCES task(id)
                )
            """))
            conn.execute(text(f"INSERT INTO task_new ({cols}) SELECT {cols} FROM task"))
            conn.execute(text("DROP TABLE task"))
            conn.execute(text("ALTER TABLE task_new RENAME TO task"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_task_project_id ON task (project_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_task_depends_on_id ON task (depends_on_id)"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()

        # Migrate task.resource_id → taskresource join table (idempotent via INSERT OR IGNORE).
        # The taskresource table is created by create_db_and_tables; this only copies data
        # from the old single-resource FK column on DBs that pre-date multi-resource support.
        result = conn.execute(text("PRAGMA table_info(task)"))
        if "resource_id" in {row[1] for row in result}:
            conn.execute(text("""
                INSERT OR IGNORE INTO taskresource (task_id, resource_id)
                SELECT id, resource_id FROM task WHERE resource_id IS NOT NULL
            """))
            conn.commit()

        result = conn.execute(text("PRAGMA table_info(appsettings)"))
        appsettings_cols = {row[1] for row in result}
        if "caldav_default_project_id" not in appsettings_cols:
            conn.execute(text("ALTER TABLE appsettings ADD COLUMN caldav_default_project_id INTEGER REFERENCES project(id)"))
            conn.commit()

        result = conn.execute(text("PRAGMA table_info(project)"))
        project_cols2 = {row[1] for row in result}
        if "priority" not in project_cols2:
            conn.execute(text("ALTER TABLE project ADD COLUMN priority INTEGER NOT NULL DEFAULT 3"))
            conn.commit()
        if "directory" in project_cols2:
            # Merge directory into folder (folder takes precedence if already set), then drop.
            conn.execute(text(
                "UPDATE project SET folder = directory WHERE folder IS NULL AND directory IS NOT NULL"
            ))
            conn.execute(text("ALTER TABLE project DROP COLUMN directory"))
            conn.commit()

        result = conn.execute(text("PRAGMA table_info(task)"))
        task_cols2 = {row[1] for row in result}
        if "command" not in task_cols2:
            conn.execute(text("ALTER TABLE task ADD COLUMN command TEXT"))
            conn.commit()
        if "exit_code" not in task_cols2:
            conn.execute(text("ALTER TABLE task ADD COLUMN exit_code INTEGER"))
            conn.commit()
        if "log_tail" not in task_cols2:
            conn.execute(text("ALTER TABLE task ADD COLUMN log_tail TEXT"))
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
