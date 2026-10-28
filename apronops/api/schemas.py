"""Validated operator commands."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
