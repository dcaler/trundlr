from datetime import date, timedelta

from sqlmodel import Session

from app.models import Project, Resource, ResourceKind, Task, TaskStatus


def seed_data(session: Session) -> None:
    """Populate the database with sample projects, resources, and tasks.

    Creates:
    - 3 projects
    - 3 humans, 1 CPU node, 1 GPU node
    - 6 demo tasks spread across projects and resources
    """
    # Projects
    project_website = Project(name="Website Redesign", description="Refresh company site")
    project_ml = Project(name="ML Pipeline", description="Build training infrastructure")
    project_infra = Project(name="Infrastructure", description="Cloud platform upgrades")

    session.add(project_website)
    session.add(project_ml)
    session.add(project_infra)
    session.flush()  # Assign IDs without committing

    # Resources: humans
    alice = Resource(name="Alice", kind=ResourceKind.human, capacity=8.0)
    bob = Resource(name="Bob", kind=ResourceKind.human, capacity=8.0)
    charlie = Resource(name="Charlie", kind=ResourceKind.human, capacity=8.0)

    # Resources: compute
    cpu_node = Resource(name="CPU Node 1", kind=ResourceKind.cpu, capacity=4.0)
    gpu_node = Resource(name="GPU Node 1", kind=ResourceKind.gpu, capacity=2.0)

    session.add(alice)
    session.add(bob)
    session.add(charlie)
    session.add(cpu_node)
    session.add(gpu_node)
    session.flush()

    # Tasks
    today = date.today()
    tomorrow = today + timedelta(days=1)
    next_week = today + timedelta(days=7)
    next_month = today + timedelta(days=30)

    # Website Redesign tasks
    task_design = Task(
        title="Design mockups",
        status=TaskStatus.in_progress,
        project_id=project_website.id,
        resource_id=alice.id,
        load=6.0,
        start_date=today,
        end_date=next_week,
    )
    task_frontend = Task(
        title="Build frontend",
        status=TaskStatus.todo,
        project_id=project_website.id,
        resource_id=bob.id,
        load=8.0,
        start_date=next_week,
        end_date=next_month,
    )

    # ML Pipeline tasks
    task_data_prep = Task(
        title="Data preparation",
        status=TaskStatus.in_progress,
        project_id=project_ml.id,
        resource_id=charlie.id,
        load=4.0,
        start_date=today,
        end_date=next_week,
    )
    task_training = Task(
        title="Model training",
        status=TaskStatus.blocked,
        project_id=project_ml.id,
        resource_id=gpu_node.id,
        load=2.0,
        start_date=next_week,
        end_date=next_month,
    )

    # Infrastructure tasks
    task_network = Task(
        title="Network optimization",
        status=TaskStatus.todo,
        project_id=project_infra.id,
        resource_id=bob.id,
        load=5.0,
        start_date=tomorrow,
        end_date=next_week,
    )
    task_monitoring = Task(
        title="Monitoring setup",
        status=TaskStatus.todo,
        project_id=project_infra.id,
        resource_id=None,  # Unassigned
        load=3.0,
        start_date=next_week,
        end_date=next_month,
    )

    session.add(task_design)
    session.add(task_frontend)
    session.add(task_data_prep)
    session.add(task_training)
    session.add(task_network)
    session.add(task_monitoring)

    session.commit()
