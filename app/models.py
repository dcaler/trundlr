from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class ResourceKind(str, Enum):
    human = "human"
    ai = "ai"
    cpu = "cpu"
    gpu = "gpu"


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"
    failed = "failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = Field(default=None)
    folder: Optional[str] = Field(default=None)  # runner working dir: must be an absolute, existing path; also shown as a grouping label
    archived: bool = Field(default=False)
    priority: int = Field(default=3)  # 1=Critical 2=High 3=Medium 4=Low
    created_at: datetime = Field(default_factory=_utcnow)

    tasks: list["Task"] = Relationship(back_populates="project")


class Resource(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    kind: ResourceKind
    # Availability window applies to all resource kinds; bit 0=Mon … bit 6=Sun.
    available_from: str = Field(default="09:00")   # "HH:MM"
    available_to: str = Field(default="17:00")     # "HH:MM"
    available_days: int = Field(default=31)        # Mon-Fri bitmask


class AppSettings(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    timezone: str = Field(default="UTC")
    caldav_default_project_id: Optional[int] = Field(default=None, foreign_key="project.id")


class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: Optional[str] = Field(default=None)
    status: TaskStatus = Field(default=TaskStatus.todo)
    # Both nullable: a task may be unscheduled, and an open-ended task
    # (start with no end) is a valid, supported state for the engine.
    # Stored as datetime for hour-precision scheduling.
    start_date: Optional[datetime] = Field(default=None)
    end_date: Optional[datetime] = Field(default=None)
    duration: Optional[float] = Field(default=None)  # total hours (informational)
    # Execution fields — populated by the runner daemon.
    command:   Optional[str] = Field(default=None)  # shell command to execute
    exit_code: Optional[int] = Field(default=None)  # process exit code after completion
    log_tail:  Optional[str] = Field(default=None)  # last N lines of stdout+stderr

    # A task must belong to a project; resource assignments live in TaskResource.
    project_id: int = Field(foreign_key="project.id", nullable=False, index=True)
    # Optional predecessor: this task should start after depends_on finishes.
    depends_on_id: Optional[int] = Field(
        default=None, foreign_key="task.id", index=True
    )

    project: Optional[Project] = Relationship(back_populates="tasks")


class TaskResource(SQLModel, table=True):
    """Join table: a task may be assigned to multiple resources."""
    task_id: int = Field(foreign_key="task.id", primary_key=True)
    resource_id: int = Field(foreign_key="resource.id", primary_key=True)
