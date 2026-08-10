from datetime import date, timedelta

from sqlmodel import Session

from app.models import Project, Resource, ResourceKind, Task, TaskResource, TaskStatus


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
    session.flush()

    # Resources: humans
    alice = Resource(name="Alice", kind=ResourceKind.human,
                     available_from="09:00", available_to="17:00", available_days=31)
    bob = Resource(name="Bob", kind=ResourceKind.human,
                   available_from="09:00", available_to="17:00", available_days=31)
    charlie = Resource(name="Charlie", kind=ResourceKind.human,
                       available_from="09:00", available_to="17:00", available_days=31)

    # Resources: compute (same availability model as humans)
    cpu_node = Resource(name="CPU Node 1", kind=ResourceKind.cpu,
                        available_from="00:00", available_to="23:59", available_days=127)
    gpu_node = Resource(name="GPU Node 1", kind=ResourceKind.gpu,
                        available_from="00:00", available_to="23:59", available_days=127)

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
        start_date=today,
        end_date=next_week,
    )
    task_frontend = Task(
        title="Build frontend",
        status=TaskStatus.todo,
        project_id=project_website.id,
        start_date=next_week,
        end_date=next_month,
    )

    # ML Pipeline tasks
    task_data_prep = Task(
        title="Data preparation",
        status=TaskStatus.in_progress,
        project_id=project_ml.id,
        start_date=today,
        end_date=next_week,
    )
    task_training = Task(
        title="Model training",
        status=TaskStatus.blocked,
        project_id=project_ml.id,
        start_date=next_week,
        end_date=next_month,
    )

    # Infrastructure tasks
    task_network = Task(
        title="Network optimization",
        status=TaskStatus.todo,
        project_id=project_infra.id,
        start_date=tomorrow,
        end_date=next_week,
    )
    task_monitoring = Task(
        title="Monitoring setup",
        status=TaskStatus.todo,
        project_id=project_infra.id,
        start_date=next_week,
        end_date=next_month,
    )

    session.add_all([task_design, task_frontend, task_data_prep,
                     task_training, task_network, task_monitoring])
    session.flush()

    # Resource assignments via join table
    session.add(TaskResource(task_id=task_design.id, resource_id=alice.id))
    session.add(TaskResource(task_id=task_frontend.id, resource_id=bob.id))
    session.add(TaskResource(task_id=task_data_prep.id, resource_id=charlie.id))
    session.add(TaskResource(task_id=task_training.id, resource_id=gpu_node.id))
    session.add(TaskResource(task_id=task_network.id, resource_id=bob.id))
    # task_monitoring is intentionally unassigned

    session.commit()
