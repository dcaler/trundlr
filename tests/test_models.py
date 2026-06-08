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
        load=4.0,
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
    human = Resource(name="Bob", kind=ResourceKind.human, capacity=8.0)
    cpu = Resource(name="node-1", kind=ResourceKind.cpu, capacity=16.0)
    gpu = Resource(name="dgx-1", kind=ResourceKind.gpu, capacity=4.0)
    session.add_all([human, cpu, gpu])
    session.commit()

    kinds = {r.name: r.kind for r in (human, cpu, gpu)}
    assert kinds == {
        "Bob": ResourceKind.human,
        "node-1": ResourceKind.cpu,
        "dgx-1": ResourceKind.gpu,
    }


def test_unified_load_interface(session):
    """A human task (hours/day) and a GPU task (slots) share one numeric
    interface; the model stores both as plain floats."""
    project = Project(name="Shared")
    human = Resource(name="Carol", kind=ResourceKind.human,
                     available_from="09:00", available_to="17:00", available_days=31)
    gpu = Resource(name="dgx-2", kind=ResourceKind.gpu, capacity=4.0)
    session.add_all([project, human, gpu])
    session.commit()

    human_task = Task(title="Design", project_id=project.id, load=6.0)
    gpu_task = Task(title="Train", project_id=project.id, load=2.0)
    session.add_all([human_task, gpu_task])
    session.commit()

    assert human_task.load == 6.0  # 6 hours/day
    assert gpu_task.load == 2.0    # 2 of 4 slots


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
