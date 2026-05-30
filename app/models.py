from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class ResourceKind(str, Enum):
    """What a resource is, which fixes the unit of its capacity/load.

    human / ai -> capacity & load are measured in hours/day (derived from availability)
    cpu / gpu  -> capacity & load are measured in parallel slots
    """

    human = "human"
    ai = "ai"
    cpu = "cpu"
    gpu = "gpu"


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = Field(default=None)
    folder: Optional[str] = Field(default=None)
    archived: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)

    tasks: list["Task"] = Relationship(back_populates="project")


class Resource(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    kind: ResourceKind
    # cpu/gpu: parallel slots (explicit). human/ai: None — derived from availability.
    capacity: Optional[float] = Field(default=None)
    # Human/AI availability: time window + day-of-week bitmask (bit 0=Mon … bit 6=Sun).
    available_from: Optional[str] = Field(default=None)  # "HH:MM"
    available_to: Optional[str] = Field(default=None)    # "HH:MM"
    available_days: Optional[int] = Field(default=None)  # bitmask


class AppSettings(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    timezone: str = Field(default="UTC")


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
    # Units consumed per day while active, in the same unit as the
    # assigned resource's capacity (human -> hours/day; compute -> slots).
    load: float = Field(default=1.0)
    # Total elapsed calendar duration in hours (informational, not used by engine).
    duration: Optional[float] = Field(default=None)

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
