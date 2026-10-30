from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import random

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, field_serializer, model_validator


class Spec(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

    @field_validator('*')
    @classmethod
    def clock_precision(cls, value):
        # datetime advances at microsecond resolution; smaller deltas could stall.
        if isinstance(value, float):
            rounded = round(value, 6)
            if value != 0 and rounded == 0:
                raise ValueError('time precision is one microsecond')
            return rounded
        return value


class FlightSpec(Spec):
    id: str
    stand_id: str
    announce_s: float = Field(default=0, ge=0)
    arrival_s: float = Field(ge=0)
    turnaround_s: float = Field(gt=0)
    arrival_jitter_s: float = Field(default=0, ge=0)


class CrossingSpec(Spec):
    id: str
    crossing_id: str
    aircraft_id: str
    announce_s: float = Field(default=0, ge=0)
    predicted_start_s: float = Field(ge=0)
    duration_s: float = Field(gt=0)
    request_shift_s: float = 0
    shift_jitter_s: float = Field(default=0, ge=0)


class ClosureSpec(Spec):
    id: str
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    from_node: str
    to_node: str


class FaultSpec(Spec):
    id: str
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    vehicle_id: str
    anomaly_type: Literal['communication_lost', 'position_quality_degraded']


class TaskSpec(Spec):
    id: str
    type: Literal['inspection', 'baggage_tow', 'catering_supply']
    origin: str
    destination: str
    release_s: float = Field(default=0, ge=0)
    deadline_s: float = Field(gt=0)
    service_duration_s: float = Field(default=60, gt=0)
    priority: int = Field(default=3, ge=1, le=5)


class DepotSpec(Spec):
    capacity: int = Field(default=2, ge=1, le=100, strict=True)
    pickup_s: float = Field(default=10, gt=0)
    recovery_after_s: float = Field(default=30, gt=0)


class VehicleSpec(Spec):
    id: str = Field(min_length=1)
    type: Literal['tractor','shuttle','catering','amr_inspect']
    home_node: str
    speed_mps: float = Field(gt=0, le=15)
    battery: float = Field(default=100, ge=0, le=100)


class AircraftSpec(Spec):
    id: str = Field(min_length=1)
    flight_id: str
    entry_node: str = '900'
    exit_node: str = '901'
    taxi_start_s: float = Field(ge=0)
    speed_mps: float = Field(default=4, gt=0, le=10)
    length_m: float = Field(default=12, gt=0, le=40)


class Scenario(Spec):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r'^[A-Za-z0-9_-]+$')
    description: str = ''
    start_time: datetime = datetime(2026, 1, 1)  # Internal clock uses naive UTC.
    horizon_s: float = Field(default=3600, gt=0, le=86400)
    step_s: float = Field(default=.5, gt=0, le=.5)
    sample_s: float = Field(default=5., ge=.5)
    flights: list[FlightSpec] = Field(default_factory=list)
    crossings: list[CrossingSpec] = Field(default_factory=list)
    closures: list[ClosureSpec] = Field(default_factory=list)
    faults: list[FaultSpec] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(default_factory=list)
    depots: dict[str, DepotSpec] = Field(default_factory=dict)
    vehicles: list[VehicleSpec] | None = Field(default=None, min_length=1)
    aircraft: list[AircraftSpec] = Field(default_factory=list)

    @field_validator('start_time')
    @classmethod
    def utc(cls, value):
        if value.tzinfo is None:
            raise ValueError('start_time must include timezone')
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @field_serializer('start_time', when_used='json')
    def serialize_time(self, value):
        return value.replace(tzinfo=timezone.utc).isoformat()

    @model_validator(mode='after')
    def validate_schedule(self):
        for values in (self.flights, self.crossings, self.closures, self.faults, self.tasks, self.aircraft, self.vehicles or []):
            if len({v.id for v in values}) != len(values):
                raise ValueError('duplicate IDs in scenario')
        flight_by_id={f.id:f for f in self.flights}
        if len({a.flight_id for a in self.aircraft}) != len(self.aircraft):
            raise ValueError('only one aircraft per flight')
        for a in self.aircraft:
            if a.flight_id not in flight_by_id or not flight_by_id[a.flight_id].announce_s <= a.taxi_start_s <= self.horizon_s:
                raise ValueError('aircraft requires announced flight and taxi start within horizon')
        for f in self.flights:
            if f.arrival_s-f.arrival_jitter_s < f.announce_s:
                raise ValueError('flight may arrive before announcement')
        for c in self.crossings:
            if c.predicted_start_s+c.request_shift_s-c.shift_jitter_s < c.announce_s:
                raise ValueError('crossing may request passage before announcement')
        for disturbance in [*self.closures, *self.faults]:
            if disturbance.end_s <= disturbance.start_s or disturbance.end_s > self.horizon_s:
                raise ValueError('disturbance requires start < end <= horizon')
        for item in [*self.flights, *self.crossings]:
            if item.announce_s > self.horizon_s:
                raise ValueError('announcement outside horizon')
        for task in self.tasks:
            if task.deadline_s <= task.release_s:
                raise ValueError('task deadline must follow release')
        # Overlapping manual closures/faults have shared alert semantics; reject
        # ambiguous paired recovery events rather than silently resolving early.
        groups = {}
        for c in self.closures:
            groups.setdefault(('road', *sorted((c.from_node, c.to_node))), []).append((c.start_s, c.end_s))
        for f in self.faults:
            groups.setdefault(('fault', f.vehicle_id, f.anomaly_type), []).append((f.start_s, f.end_s))
        for intervals in groups.values():
            intervals.sort()
            if any(a[1] >= b[0] for a, b in zip(intervals, intervals[1:])):
                raise ValueError('overlapping or touching duplicate disturbance intervals')
        return self


def read_scenario(path):
    return Scenario.model_validate(yaml.safe_load(Path(path).read_text(encoding='utf-8')))


def realize(scenario: Scenario, seed: int):
    """Generate all exogenous inputs once, independently of policy execution."""
    rng = random.Random(seed)
    events = []
    for spec in scenario.flights:
        payload = spec.model_dump()
        payload['arrival_s'] = round(spec.arrival_s + rng.uniform(-spec.arrival_jitter_s, spec.arrival_jitter_s), 6)
        events.append({'at_s': spec.announce_s, 'kind': 'flight', 'id': spec.id, 'payload': payload})
    for spec in scenario.crossings:
        payload = spec.model_dump()
        payload['requested_start_s'] = round(spec.predicted_start_s+spec.request_shift_s+rng.uniform(-spec.shift_jitter_s, spec.shift_jitter_s), 6)
        events.append({'at_s': spec.announce_s, 'kind': 'crossing', 'id': spec.id, 'payload': payload})
    for spec in scenario.tasks:
        events.append({'at_s': 0., 'kind': 'task', 'id': spec.id, 'payload': spec.model_dump()})
    for spec in scenario.aircraft:
        announce = next(f.announce_s for f in scenario.flights if f.id==spec.flight_id)
        events.append({'at_s':announce,'kind':'flight_aircraft','id':spec.id,'payload':spec.model_dump()})
    for kind, specs in (('closure', scenario.closures), ('fault', scenario.faults)):
        for spec in specs:
            events.extend([{'at_s': spec.start_s, 'kind': kind, 'id': spec.id, 'payload': spec.model_dump()},
                           {'at_s': spec.end_s, 'kind': kind+'_end', 'id': spec.id, 'payload': spec.model_dump()}])
    return sorted(events, key=lambda e: (e['at_s'], e['kind'], e['id']))
