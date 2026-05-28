from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class ResourceKind(str, Enum):
    """What a resource is, which fixes the unit of its capacity/load.

    human -> capacity & load are measured in hours/day
    cpu / gpu -> capacity & load are measured in parallel slots
    """

    human = "human"
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
    created_at: datetime = Field(default_factory=_utcnow)

    tasks: list["Task"] = Relationship(back_populates="project")


class Resource(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    kind: ResourceKind
    # Units available per day. Unit is fixed by `kind`:
    # human -> hours/day; cpu/gpu -> parallel slots. The scheduling
    # engine compares this to summed task load on a per-day basis.
    capacity: float

    tasks: list["Task"] = Relationship(back_populates="resource")


class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    status: TaskStatus = Field(default=TaskStatus.todo)
    # Both nullable: a task may be unscheduled, and an open-ended task
    # (start with no end) is a valid, supported state for the engine.
    start_date: Optional[date] = Field(default=None)
    end_date: Optional[date] = Field(default=None)
    # Units consumed per day while active, in the same unit as the
    # assigned resource's capacity (human -> hours/day; compute -> slots).
    load: float = Field(default=1.0)

    # A task must belong to a project; it need not be assigned a resource.
    project_id: int = Field(foreign_key="project.id", nullable=False, index=True)
    resource_id: Optional[int] = Field(
        default=None, foreign_key="resource.id", index=True
    )

    project: Optional[Project] = Relationship(back_populates="tasks")
    resource: Optional[Resource] = Relationship(back_populates="tasks")
