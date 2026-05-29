from datetime import date, datetime
from typing import Annotated, Optional

from pydantic import BaseModel, Field, model_validator

from app.models import ResourceKind, TaskStatus
from app.validation import MAX_DB_INT

# A capacity/load value: strictly positive and finite. allow_inf_nan=False
# rejects the Infinity/NaN tokens that Python's JSON parser accepts but which
# cannot be serialized back into a response (a divide-by-zero / 500 hazard).
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]

# A non-empty, trimmed display string (project/resource name, task title).
NonEmptyStr = Annotated[str, Field(min_length=1)]

# A foreign-key id supplied in a request body, bounded to SQLite's int range.
BodyId = Annotated[int, Field(ge=1, le=MAX_DB_INT)]
OptionalBodyId = Annotated[Optional[int], Field(ge=1, le=MAX_DB_INT)]


class ProjectCreate(BaseModel):
    name: NonEmptyStr
    description: Optional[str] = None
    folder: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[NonEmptyStr] = None
    description: Optional[str] = None
    folder: Optional[str] = None


class ProjectRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    folder: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ResourceCreate(BaseModel):
    name: NonEmptyStr
    kind: ResourceKind
    capacity: PositiveFloat


class ResourceUpdate(BaseModel):
    name: Optional[NonEmptyStr] = None
    kind: Optional[ResourceKind] = None
    capacity: Optional[PositiveFloat] = None


class ResourceRead(BaseModel):
    id: int
    name: str
    kind: ResourceKind
    capacity: float

    model_config = {"from_attributes": True}


def _validate_date_range(start: Optional[datetime], end: Optional[datetime]) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError("end_date must not be before start_date")


class TaskCreate(BaseModel):
    title: NonEmptyStr
    description: Optional[str] = None
    project_id: BodyId
    resource_id: OptionalBodyId = None
    depends_on_id: OptionalBodyId = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    load: PositiveFloat = 1.0
    duration: Optional[PositiveFloat] = None
    status: TaskStatus = TaskStatus.todo

    @model_validator(mode="after")
    def end_after_start(self) -> "TaskCreate":
        _validate_date_range(self.start_date, self.end_date)
        return self


class TaskUpdate(BaseModel):
    title: Optional[NonEmptyStr] = None
    description: Optional[str] = None
    resource_id: OptionalBodyId = None
    depends_on_id: OptionalBodyId = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    load: Optional[PositiveFloat] = None
    duration: Optional[PositiveFloat] = None
    status: Optional[TaskStatus] = None

    @model_validator(mode="after")
    def end_after_start(self) -> "TaskUpdate":
        _validate_date_range(self.start_date, self.end_date)
        return self


class TaskRead(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    status: TaskStatus
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    load: float
    duration: Optional[float] = None
    project_id: int
    resource_id: Optional[int] = None
    depends_on_id: Optional[int] = None

    model_config = {"from_attributes": True}


class DayUtilizationRead(BaseModel):
    day: date
    committed: float
    capacity: float
    utilization: float

    model_config = {"from_attributes": True}


class ConflictTaskRead(BaseModel):
    id: int
    title: str

    model_config = {"from_attributes": True}


class ConflictRead(BaseModel):
    day: date
    committed: float
    capacity: float
    overage: float
    tasks: list[ConflictTaskRead]

    model_config = {"from_attributes": True}


class ResourceScheduleRead(BaseModel):
    resource_id: int
    resource_name: str
    days: list[DayUtilizationRead]
