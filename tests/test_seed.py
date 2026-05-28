from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Project, Resource, Task
from app.seed import seed_data


def test_seed_creates_expected_row_counts():
    """Verify seed populates database with correct entity counts."""
    # Create in-memory temp DB
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    # Seed the database
    with Session(engine) as session:
        seed_data(session)

    # Verify counts
    with Session(engine) as session:
        project_count = session.query(Project).count()
        resource_count = session.query(Resource).count()
        task_count = session.query(Task).count()

        assert project_count == 3, f"Expected 3 projects, got {project_count}"
        assert resource_count == 5, f"Expected 5 resources, got {resource_count}"
        assert task_count == 6, f"Expected 6 tasks, got {task_count}"


def test_seed_relationships_resolve():
    """Verify tasks link correctly to projects and resources."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        seed_data(session)

    with Session(engine) as session:
        # Fetch a task with a resource
        task = session.query(Task).filter(Task.title == "Design mockups").first()
        assert task is not None
        assert task.project is not None
        assert task.resource is not None
        assert task.project.name == "Website Redesign"
        assert task.resource.name == "Alice"

        # Fetch a task without a resource (unassigned)
        unassigned_task = (
            session.query(Task).filter(Task.title == "Monitoring setup").first()
        )
        assert unassigned_task is not None
        assert unassigned_task.resource is None
        assert unassigned_task.project.name == "Infrastructure"


def test_seed_resource_kinds():
    """Verify human and compute resources are created with correct kinds."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        seed_data(session)

    with Session(engine) as session:
        humans = session.query(Resource).filter_by(kind="human").count()
        cpus = session.query(Resource).filter_by(kind="cpu").count()
        gpus = session.query(Resource).filter_by(kind="gpu").count()

        assert humans == 3, f"Expected 3 humans, got {humans}"
        assert cpus == 1, f"Expected 1 CPU, got {cpus}"
        assert gpus == 1, f"Expected 1 GPU, got {gpus}"
