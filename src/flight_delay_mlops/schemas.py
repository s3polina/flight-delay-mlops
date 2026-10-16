from datetime import date

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


class ProcessRequest(BaseModel):
    flights: list[Flight] = Field(min_length=1, max_length=1000)


class Prediction(BaseModel):
    delay_probability: float
    is_delayed: bool


class ModelInfo(BaseModel):
    name: str
    alias: str
    version: str
    run_id: str


class ProcessResponse(BaseModel):
    model: ModelInfo
    predictions: list[Prediction]
