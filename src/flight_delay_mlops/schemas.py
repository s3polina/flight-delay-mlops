from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class Flight(BaseModel):
    """Данные рейса, известные до вылета."""

    flight_date: date = Field(examples=["2013-12-20"])
    sched_dep_time: int = Field(ge=0, le=2359, description="HHMM", examples=[1830])
    sched_arr_time: int = Field(ge=0, le=2359, description="HHMM", examples=[2135])
    carrier: str = Field(min_length=2, max_length=3, examples=["EV"])
    origin: str = Field(min_length=3, max_length=3, examples=["EWR"])
    dest: str = Field(min_length=3, max_length=3, examples=["ATL"])
    distance: float = Field(gt=0, description="мили", examples=[746])


class TaskMessage(BaseModel):
    task_id: UUID
    source: str
    created_at: datetime
    flight: Flight


class TaskAccepted(BaseModel):
    task_id: UUID
    status: str


class TaskStatus(BaseModel):
    task_id: UUID
    status: str
    delay_probability: float | None = None
    is_delayed: bool | None = None
    model_version: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
