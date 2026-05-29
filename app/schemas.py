import re
from datetime import date, datetime
from typing import Annotated, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

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
    archived: Optional[bool] = None


class ProjectRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    folder: Optional[str] = None
    archived: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _validate_time_str(value: str, field: str) -> None:
    if not _TIME_RE.match(value):
        raise ValueError(f"{field} must be in HH:MM format")
    h, m = map(int, value.split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"{field} is not a valid time")


class ResourceCreate(BaseModel):
    name: NonEmptyStr
    kind: ResourceKind
    capacity: Optional[PositiveFloat] = None
    available_from: Optional[str] = None
    available_to: Optional[str] = None
    available_days: Optional[int] = None  # bitmask bit 0=Mon … bit 6=Sun

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "ResourceCreate":
        if self.kind in (ResourceKind.human, ResourceKind.ai):
            if self.capacity is not None:
                raise ValueError("capacity is not used for human/AI resources; use available_from/available_to/available_days")
            if not self.available_from or not self.available_to or self.available_days is None:
                raise ValueError("human/AI resources require available_from, available_to, and available_days")
            _validate_time_str(self.available_from, "available_from")
            _validate_time_str(self.available_to, "available_to")
            fh, fm = map(int, self.available_from.split(":"))
            th, tm = map(int, self.available_to.split(":"))
            if (th * 60 + tm) <= (fh * 60 + fm):
                raise ValueError("available_to must be later than available_from")
            if not (1 <= self.available_days <= 127):
                raise ValueError("available_days must be a bitmask between 1 and 127")
        else:
            if self.capacity is None:
                raise ValueError(f"{self.kind} resources require capacity")
            if any(f is not None for f in [self.available_from, self.available_to, self.available_days]):
                raise ValueError("availability fields are only valid for human resources")
        return self


class ResourceUpdate(BaseModel):
    name: Optional[NonEmptyStr] = None
    kind: Optional[ResourceKind] = None
    capacity: Optional[PositiveFloat] = None
    available_from: Optional[str] = None
    available_to: Optional[str] = None
    available_days: Optional[int] = None


class ResourceRead(BaseModel):
    id: int
    name: str
    kind: ResourceKind
    capacity: Optional[float] = None
    available_from: Optional[str] = None
    available_to: Optional[str] = None
    available_days: Optional[int] = None

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


class SettingsRead(BaseModel):
    timezone: str

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    timezone: str

    @field_validator("timezone")
    @classmethod
    def valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"'{v}' is not a valid IANA timezone")
        return v
