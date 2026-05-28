from datetime import date, datetime
from typing import Annotated, Optional

from pydantic import BaseModel, Field, model_validator

from app.models import ResourceKind, TaskStatus

PositiveFloat = Annotated[float, Field(gt=0)]


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class ProjectRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ResourceCreate(BaseModel):
    name: str
    kind: ResourceKind
    capacity: PositiveFloat


class ResourceUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[ResourceKind] = None
    capacity: Optional[PositiveFloat] = None


class ResourceRead(BaseModel):
    id: int
    name: str
    kind: ResourceKind
    capacity: float

    model_config = {"from_attributes": True}


def _validate_date_range(start: Optional[date], end: Optional[date]) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError("end_date must not be before start_date")


class TaskCreate(BaseModel):
    title: str
    project_id: int
    resource_id: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    load: PositiveFloat = 1.0
    status: TaskStatus = TaskStatus.todo

    @model_validator(mode="after")
    def end_after_start(self) -> "TaskCreate":
        _validate_date_range(self.start_date, self.end_date)
        return self


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    resource_id: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    load: Optional[PositiveFloat] = None
    status: Optional[TaskStatus] = None

    @model_validator(mode="after")
    def end_after_start(self) -> "TaskUpdate":
        _validate_date_range(self.start_date, self.end_date)
        return self


class TaskRead(BaseModel):
    id: int
    title: str
    status: TaskStatus
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    load: float
    project_id: int
    resource_id: Optional[int] = None

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
