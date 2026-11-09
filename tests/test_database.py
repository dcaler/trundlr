import tempfile
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlmodel import Session, select

from app.database import create_db_and_tables, get_engine, get_session
from app.models import Project, Resource, ResourceKind, Task


@pytest.fixture
def temp_db():
    """Fixture providing a temporary SQLite database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield f"sqlite:///{db_path}"


def test_startup_creates_tables_in_temp_db(temp_db):
    """Verify startup creates tables in a temp DB file."""
    engine = get_engine(temp_db)
    create_db_and_tables(engine)

    # Inspect the engine to verify tables exist
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    assert "project" in tables
    assert "resource" in tables
    assert "task" in tables
    assert "taskresource" in tables
    assert "appsettings" in tables


def test_session_dependency_yields_working_session(temp_db):
    """Verify session dependency yields a working session."""
    engine = get_engine(temp_db)
    create_db_and_tables(engine)

    # Simulate the dependency injection by getting a session from the generator
    session_gen = get_session(engine)
    session = next(session_gen)

    try:
        # Create and insert a project
        project = Project(name="Test Project", description="A test")
        session.add(project)
        session.commit()
        session.refresh(project)

        # Verify we can query it back
        statement = select(Project).where(Project.name == "Test Project")
        found = session.exec(statement).first()

        assert found is not None
        assert found.id == project.id
        assert found.name == "Test Project"
        assert found.description == "A test"
    finally:
        # Clean up the session
        session.close()


def test_foreign_key_enforcement(temp_db):
    """Verify SQLite foreign key constraint enforcement is enabled."""
    engine = get_engine(temp_db)
    create_db_and_tables(engine)

    session = next(get_session(engine))

    try:
        # Attempt to create a Task with a non-existent project_id
        # This should raise an IntegrityError when we commit
        task = Task(title="Orphan Task", project_id=9999)
        session.add(task)

        with pytest.raises(Exception):  # IntegrityError or similar
            session.commit()
    finally:
        session.close()


def test_session_context_manager(temp_db):
    """Verify session can be used as a context manager."""
    engine = get_engine(temp_db)
    create_db_and_tables(engine)

    # Create a project first
    with Session(engine) as session:
        project = Project(name="Context Manager Test")
        session.add(project)
        session.commit()
        session.refresh(project)
        project_id = project.id

    # Query it back using context manager
    with Session(engine) as session:
        statement = select(Project).where(Project.id == project_id)
        found = session.exec(statement).first()
        assert found is not None
        assert found.name == "Context Manager Test"


def test_migration_adds_runner_columns_to_old_db(temp_db):
    """A database from before runner_env / runner_folder / lane_settle_minutes gains them."""
    from sqlalchemy import text
    from app.database import apply_migrations

    engine = get_engine(temp_db)
    create_db_and_tables(engine)
    with engine.connect() as c:
        c.execute(text("ALTER TABLE resource DROP COLUMN runner_env"))
        c.execute(text("ALTER TABLE appsettings DROP COLUMN runner_folder"))
        c.execute(text("ALTER TABLE appsettings DROP COLUMN lane_settle_minutes"))
        c.commit()
    apply_migrations(engine)
    with engine.connect() as c:
        res_cols = {r[1] for r in c.execute(text("PRAGMA table_info(resource)"))}
        set_cols = {r[1] for r in c.execute(text("PRAGMA table_info(appsettings)"))}
    assert "runner_env" in res_cols
    assert {"runner_folder", "lane_settle_minutes"} <= set_cols


def test_migration_adds_scheduler_columns_to_old_db(temp_db):
    """A database from before age priority and sticky drains gains their columns."""
    from sqlalchemy import text
    from app.database import apply_migrations

    engine = get_engine(temp_db)
    create_db_and_tables(engine)
    with engine.connect() as c:
        c.execute(text("ALTER TABLE task DROP COLUMN eligible_since"))
        c.execute(text("ALTER TABLE task DROP COLUMN drain_since"))
        c.execute(text("ALTER TABLE appsettings DROP COLUMN age_priority_days"))
        c.commit()
    apply_migrations(engine)
    with engine.connect() as c:
        task_cols = {r[1] for r in c.execute(text("PRAGMA table_info(task)"))}
        set_cols = {r[1] for r in c.execute(text("PRAGMA table_info(appsettings)"))}
    assert {"eligible_since", "drain_since"} <= task_cols
    assert "age_priority_days" in set_cols


def test_migration_backdates_eligible_since(temp_db):
    """Ready todo tasks get a lower bound on how long they've waited: the earliest
    run of any later-created task, or their dependency's finish if later."""
    from datetime import datetime
    from sqlalchemy import text
    from app.database import apply_migrations
    from app.models import Project, Task, TaskDependency, TaskStatus

    engine = get_engine(temp_db)
    create_db_and_tables(engine)
    d = datetime
    with Session(engine) as s:
        s.add(Project(id=1, name="P"))
        s.add_all([
            Task(id=1, title="old todo", project_id=1),
            Task(id=2, title="dep", project_id=1, status=TaskStatus.done,
                 start_date=d(2026, 10, 1, 9), end_date=d(2026, 10, 3, 9)),
            Task(id=3, title="after dep", project_id=1),
            Task(id=5, title="ran", project_id=1, status=TaskStatus.in_progress,
                 start_date=d(2026, 10, 2, 9), end_date=d(2026, 10, 9, 9)),
            Task(id=6, title="newest todo", project_id=1),
        ])
        s.commit()
        s.add(Task(id=4, title="waits", project_id=1))
        s.commit()
        s.add_all([TaskDependency(task_id=3, depends_on_id=2),
                   TaskDependency(task_id=4, depends_on_id=5)])
        s.commit()
    with engine.connect() as c:
        c.execute(text("ALTER TABLE task DROP COLUMN eligible_since"))
        c.commit()
    apply_migrations(engine)
    with Session(engine) as s:
        got = {t.id: t.eligible_since for t in s.exec(select(Task)).all()}
    assert got[1] == d(2026, 10, 1, 9)    # task 2 ran on the 1st, so 1 existed by then
    assert got[3] == d(2026, 10, 3, 9)    # bound is the 2nd, but its dependency finished on the 3rd
    assert got[4] is None                 # dependency still running
    assert got[6] is None                 # nothing later has run: re-flow stamps it
    assert got[2] is None and got[5] is None


def test_migration_moves_single_dependency_to_join_table(temp_db):
    """An old DB's task.depends_on_id becomes a taskdependency row, once: the
    column is emptied, so a re-run doesn't restore a link removed since."""
    from sqlalchemy import text
    from app.database import apply_migrations
    from app.models import Project, Task, TaskDependency

    engine = get_engine(temp_db)
    create_db_and_tables(engine)
    with Session(engine) as s:
        s.add(Project(id=1, name="P"))
        s.add_all([Task(id=i, title=f"t{i}", project_id=1) for i in (1, 2, 3)])
        s.commit()
    with engine.connect() as c:
        c.execute(text("ALTER TABLE task ADD COLUMN depends_on_id INTEGER REFERENCES task(id)"))
        c.execute(text("UPDATE task SET depends_on_id = 1 WHERE id = 2"))
        c.execute(text("UPDATE task SET depends_on_id = 2 WHERE id = 3"))
        c.commit()
    apply_migrations(engine)
    with Session(engine) as s:
        links = {(r.task_id, r.depends_on_id) for r in s.exec(select(TaskDependency)).all()}
        assert links == {(2, 1), (3, 2)}
        s.delete(s.get(TaskDependency, (3, 2)))
        s.commit()
    apply_migrations(engine)
    with Session(engine) as s:
        links = {(r.task_id, r.depends_on_id) for r in s.exec(select(TaskDependency)).all()}
    assert links == {(2, 1)}
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM task WHERE depends_on_id IS NOT NULL")).scalar() == 0


def test_migration_adds_task_pool_id(temp_db):
    """pool_id is a foreign key, which SQLite can't drop, so rebuild an old task table."""
    from sqlalchemy import text
    from app.database import apply_migrations

    engine = get_engine(temp_db)
    create_db_and_tables(engine)
    with engine.connect() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(task)")) if r[1] != "pool_id"]
        c.execute(text("PRAGMA foreign_keys=OFF"))
        c.execute(text(f"CREATE TABLE task_old AS SELECT {', '.join(cols)} FROM task"))
        c.execute(text("DROP TABLE task"))
        c.execute(text("ALTER TABLE task_old RENAME TO task"))
        c.commit()
    apply_migrations(engine)
    with engine.connect() as c:
        task_cols = {r[1] for r in c.execute(text("PRAGMA table_info(task)"))}
        tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "pool_id" in task_cols
    assert "resourcepool" in tables
