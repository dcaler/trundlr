from datetime import date, datetime, timezone
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
    # When true, re-align skips this task and schedules around its existing dates.
    pinned: bool = Field(default=False)

    project: Optional[Project] = Relationship(back_populates="tasks")


class TaskResource(SQLModel, table=True):
    """Join table: a task may be assigned to multiple resources."""
    task_id: int = Field(foreign_key="task.id", primary_key=True)
    resource_id: int = Field(foreign_key="resource.id", primary_key=True)


class CycleTemplate(SQLModel, table=True):
    """A reusable bundle of tasks ("a cycle") instantiated onto a project.

    E.g. a "Lit Review" cycle whose steps are Init → Gather → Collect → Draft →
    Review. Steps, their durations, and resource assignments are configured once
    in settings and are identical across every instantiation.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    created_at: datetime = Field(default_factory=_utcnow)

    steps: list["CycleStep"] = Relationship(
        back_populates="template",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "order_by": "CycleStep.position"},
    )


class CycleStep(SQLModel, table=True):
    """One ordered step within a CycleTemplate.

    On instantiation each step becomes a Task titled "<title> <n>" (n = the cycle
    number), chained via depends_on to the previous step's task.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    template_id: int = Field(foreign_key="cycletemplate.id", index=True)
    position: int = Field(default=0)       # order within the template
    title: str
    duration: Optional[float] = Field(default=None)  # estimated hours
    command: Optional[str] = Field(default=None)     # shell command (cpu/gpu/runner tasks)

    template: Optional[CycleTemplate] = Relationship(back_populates="steps")


class CycleStepResource(SQLModel, table=True):
    """Join table: a cycle step may be assigned to multiple resources."""
    step_id: int = Field(foreign_key="cyclestep.id", primary_key=True)
    resource_id: int = Field(foreign_key="resource.id", primary_key=True)


class ResourceWindow(SQLModel, table=True):
    """Recurring weekly availability window for a resource. 0=Mon … 6=Sun.

    When any windows exist for a resource, they replace the simple
    available_from/available_to/available_days fields for scheduling purposes.
    Multiple windows on the same day are each an independent available slot.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    resource_id: int = Field(foreign_key="resource.id", index=True)
    day_of_week: int   # 0=Mon … 6=Sun
    from_time: str     # "HH:MM"
    to_time: str       # "HH:MM"


class ResourceBlockout(SQLModel, table=True):
    """A date-range exception that blocks a resource regardless of windows.

    from_time/to_time = None means the entire day is blocked.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    resource_id: int = Field(foreign_key="resource.id", index=True)
    start_date: date
    end_date: date
    from_time: Optional[str] = Field(default=None)   # None → full day
    to_time: Optional[str] = Field(default=None)
    note: Optional[str] = Field(default=None)
