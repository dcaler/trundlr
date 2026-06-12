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
    priority: int = Field(default=3, ge=1, le=4)


class ProjectUpdate(BaseModel):
    name: Optional[NonEmptyStr] = None
    description: Optional[str] = None
    folder: Optional[str] = None
    archived: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=1, le=4)


class ProjectRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    folder: Optional[str] = None
    archived: bool = False
    priority: int = 3
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
    available_from: str = "09:00"
    available_to: str = "17:00"
    available_days: int = 31  # Mon-Fri bitmask

    @model_validator(mode="after")
    def validate_availability(self) -> "ResourceCreate":
        _validate_time_str(self.available_from, "available_from")
        _validate_time_str(self.available_to, "available_to")
        fh, fm = map(int, self.available_from.split(":"))
        th, tm = map(int, self.available_to.split(":"))
        if (th * 60 + tm) <= (fh * 60 + fm):
            raise ValueError("available_to must be later than available_from")
        if not (1 <= self.available_days <= 127):
            raise ValueError("available_days must be a bitmask between 1 and 127")
        return self


class ResourceUpdate(BaseModel):
    name: Optional[NonEmptyStr] = None
    kind: Optional[ResourceKind] = None
    available_from: Optional[str] = None
    available_to: Optional[str] = None
    available_days: Optional[int] = None


class ResourceRead(BaseModel):
    id: int
    name: str
    kind: ResourceKind
    available_from: str
    available_to: str
    available_days: int

    model_config = {"from_attributes": True}


def _validate_date_range(start: Optional[datetime], end: Optional[datetime]) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError("end_date must not be before start_date")


class TaskCreate(BaseModel):
    title: NonEmptyStr
    description: Optional[str] = None
    command: Optional[str] = None
    project_id: BodyId
    resource_ids: list[BodyId] = []
    depends_on_id: OptionalBodyId = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    duration: Optional[PositiveFloat] = None
    status: TaskStatus = TaskStatus.todo

    @model_validator(mode="after")
    def end_after_start(self) -> "TaskCreate":
        _validate_date_range(self.start_date, self.end_date)
        return self


class TaskUpdate(BaseModel):
    title: Optional[NonEmptyStr] = None
    description: Optional[str] = None
    command: Optional[str] = None
    resource_ids: Optional[list[BodyId]] = None
    depends_on_id: OptionalBodyId = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    duration: Optional[PositiveFloat] = None
    status: Optional[TaskStatus] = None
    exit_code: Optional[int] = None
    log_tail: Optional[str] = None
    pinned: Optional[bool] = None
    project_id: Optional[BodyId] = None

    @model_validator(mode="after")
    def end_after_start(self) -> "TaskUpdate":
        _validate_date_range(self.start_date, self.end_date)
        return self


class TaskRead(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    command: Optional[str] = None
    status: TaskStatus
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    duration: Optional[float] = None
    exit_code: Optional[int] = None
    log_tail: Optional[str] = None
    project_id: int
    resource_ids: list[int] = []
    depends_on_id: Optional[int] = None
    pinned: bool = False

    model_config = {"from_attributes": True}


class RunnerClaimRead(TaskRead):
    """TaskRead extended with project context for the runner daemon."""
    project_directory: Optional[str] = None


class DayUtilizationRead(BaseModel):
    day: date
    committed: float  # hours of tasks assigned
    capacity: float   # hours of availability
    net: float        # capacity - committed (positive = spare, negative = over)

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


class WindowCreate(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    from_time: str
    to_time: str

    @model_validator(mode="after")
    def validate_times(self) -> "WindowCreate":
        _validate_time_str(self.from_time, "from_time")
        _validate_time_str(self.to_time, "to_time")
        fh, fm = map(int, self.from_time.split(":"))
        th, tm = map(int, self.to_time.split(":"))
        if (th * 60 + tm) <= (fh * 60 + fm):
            raise ValueError("to_time must be later than from_time")
        return self


class WindowRead(BaseModel):
    id: int
    resource_id: int
    day_of_week: int
    from_time: str
    to_time: str

    model_config = {"from_attributes": True}


class BlockoutCreate(BaseModel):
    start_date: date
    end_date: date
    from_time: Optional[str] = None
    to_time: Optional[str] = None
    note: Optional[str] = None

    @model_validator(mode="after")
    def validate_blockout(self) -> "BlockoutCreate":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if self.from_time is not None:
            _validate_time_str(self.from_time, "from_time")
        if self.to_time is not None:
            _validate_time_str(self.to_time, "to_time")
        if self.from_time is not None and self.to_time is not None:
            fh, fm = map(int, self.from_time.split(":"))
            th, tm = map(int, self.to_time.split(":"))
            if (th * 60 + tm) <= (fh * 60 + fm):
                raise ValueError("to_time must be later than from_time")
        return self


class BlockoutRead(BaseModel):
    id: int
    resource_id: int
    start_date: date
    end_date: date
    from_time: Optional[str] = None
    to_time: Optional[str] = None
    note: Optional[str] = None

    model_config = {"from_attributes": True}


class CycleStepCreate(BaseModel):
    title: NonEmptyStr
    duration: Optional[PositiveFloat] = None
    resource_ids: list[BodyId] = []
    position: int = Field(default=0, ge=0)


class CycleStepUpdate(BaseModel):
    title: Optional[NonEmptyStr] = None
    duration: Optional[PositiveFloat] = None
    resource_ids: Optional[list[BodyId]] = None
    position: Optional[int] = Field(default=None, ge=0)


class CycleStepRead(BaseModel):
    id: int
    template_id: int
    position: int
    title: str
    duration: Optional[float] = None
    resource_ids: list[int] = []

    model_config = {"from_attributes": True}


class CycleTemplateCreate(BaseModel):
    name: NonEmptyStr


class CycleTemplateUpdate(BaseModel):
    name: NonEmptyStr


class CycleTemplateRead(BaseModel):
    id: int
    name: str
    steps: list[CycleStepRead] = []

    model_config = {"from_attributes": True}


class CycleInstantiate(BaseModel):
    project_id: BodyId


class SettingsRead(BaseModel):
    timezone: str
    caldav_default_project_id: Optional[int] = None

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    timezone: str
    caldav_default_project_id: Optional[int] = None

    @field_validator("timezone")
    @classmethod
    def valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"'{v}' is not a valid IANA timezone")
        return v
