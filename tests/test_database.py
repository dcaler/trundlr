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
