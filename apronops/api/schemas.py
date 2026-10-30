"""Validated operator commands."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Command(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')


class CreateTaskRequest(Command):
    id: str = Field(min_length=1, max_length=100)
    type: str
    origin: str
    destination: str
    priority: int = Field(default=3, ge=1, le=5)
    deadline_offset_s: int = Field(default=1800, gt=0)


class ResolveAlertRequest(Command):
    resolution: str = Field(min_length=1, max_length=1000)


class CloseRoadRequest(Command):
    from_node: str
    to_node: str
    reason: str = Field(min_length=1, max_length=1000)


class FlightRequest(Command):
    id: str = Field(min_length=1, max_length=60, pattern=r'^[A-Za-z0-9_-]+$')
    stand_id: str
    arrival_at: datetime
    departure_at: datetime
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator('arrival_at', 'departure_at')
    @classmethod
    def utc_times(cls, value):
        if value.tzinfo is None:
            raise ValueError('时间必须包含时区，例如 +08:00 或 Z')
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @model_validator(mode='after')
    def ordered_times(self):
        if self.departure_at <= self.arrival_at:
            raise ValueError('离位时间必须晚于到位时间')
        return self


class OccupancyRequest(Command):
    id: str = Field(min_length=1, max_length=100)
    crossing_id: str
    aircraft_id: str = Field(min_length=1, max_length=100)
    predicted_start: datetime
    predicted_end: datetime
    requested_start: datetime
    requested_end: datetime

    @field_validator('predicted_start', 'predicted_end', 'requested_start', 'requested_end')
    @classmethod
    def utc_times(cls, value):
        return FlightRequest.utc_times(value)

    @model_validator(mode='after')
    def ordered_times(self):
        if self.predicted_end <= self.predicted_start or self.requested_end <= self.requested_start:
            raise ValueError('结束时间必须晚于开始时间')
        return self


class AircraftRequest(Command):
    id: str = Field(min_length=1, max_length=100)
    flight_id: str
    entry_node: str = '900'
    exit_node: str = '901'
    taxi_start_offset_s: float = Field(default=10, ge=0, allow_inf_nan=False)
    speed_mps: float = Field(default=4, gt=0, le=10, allow_inf_nan=False)
    length_m: float = Field(default=12, gt=0, le=40, allow_inf_nan=False)
