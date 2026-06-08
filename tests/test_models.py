from datetime import date

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Project, Resource, ResourceKind, Task, TaskResource, TaskStatus


@pytest.fixture
def session():
    # StaticPool keeps a single shared connection so the in-memory DB
    # created by create_all is the same one the session reads from.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite does not enforce foreign keys unless asked to.
    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_create_each_entity_and_relationships(session):
    project = Project(name="Apollo")
    resource = Resource(name="Alice", kind=ResourceKind.human,
                        available_from="09:00", available_to="17:00", available_days=31)
    session.add(project)
    session.add(resource)
    session.commit()

    task = Task(
        title="Write spec",
        project_id=project.id,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
    )
    session.add(task)
    session.flush()
    session.add(TaskResource(task_id=task.id, resource_id=resource.id))
    session.commit()
    session.refresh(task)

    # Project relationship resolves.
    assert task.project is project
    assert project.tasks == [task]
    # Resource assignment exists in join table.
    from sqlmodel import select
    tr = session.exec(select(TaskResource).where(TaskResource.task_id == task.id)).first()
    assert tr is not None
    assert tr.resource_id == resource.id
    # Status defaults to todo.
    assert task.status == TaskStatus.todo


def test_all_resource_kinds_persist(session):
    human = Resource(name="Bob", kind=ResourceKind.human,
                     available_from="09:00", available_to="17:00", available_days=31)
    cpu = Resource(name="node-1", kind=ResourceKind.cpu,
                   available_from="00:00", available_to="23:59", available_days=127)
    gpu = Resource(name="dgx-1", kind=ResourceKind.gpu,
                   available_from="00:00", available_to="23:59", available_days=127)
    session.add_all([human, cpu, gpu])
    session.commit()

    kinds = {r.name: r.kind for r in (human, cpu, gpu)}
    assert kinds == {
        "Bob": ResourceKind.human,
        "node-1": ResourceKind.cpu,
        "dgx-1": ResourceKind.gpu,
    }


def test_task_duration_is_optional(session):
    """A task may store an optional duration (hours); it defaults to None."""
    project = Project(name="Shared")
    session.add(project)
    session.commit()

    no_dur = Task(title="Design", project_id=project.id)
    with_dur = Task(title="Train", project_id=project.id, duration=8.0)
    session.add_all([no_dur, with_dur])
    session.commit()

    assert no_dur.duration is None
    assert with_dur.duration == 8.0


def test_task_requires_project(session):
    """NOT NULL: a task with no project_id cannot be persisted."""
    orphan = Task(title="Orphan", project_id=None)
    session.add(orphan)
    with pytest.raises(IntegrityError):
        session.commit()


def test_task_rejects_nonexistent_project(session):
    """Referential integrity: project_id must point at a real project."""
    bad = Task(title="Ghost ref", project_id=9999)
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_task_resource_is_optional(session):
    """A task may be created unassigned (no resource)."""
    project = Project(name="Backlog")
    session.add(project)
    session.commit()

    unassigned = Task(title="Someday", project_id=project.id)
    session.add(unassigned)
    session.commit()
    session.refresh(unassigned)

    from sqlmodel import select
    tr = session.exec(select(TaskResource).where(TaskResource.task_id == unassigned.id)).first()
    assert tr is None
