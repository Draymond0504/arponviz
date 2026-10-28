"""车辆 / 任务状态转移表与校验。"""
from __future__ import annotations

from .models import TaskStatus, VehicleState


class InvalidTransition(Exception):
    pass


# 车辆状态转移表：state -> 允许进入的下一状态集合
VEHICLE_TRANSITIONS: dict[VehicleState, set[VehicleState]] = {
    VehicleState.IDLE: {VehicleState.ASSIGNED, VehicleState.DEGRADED, VehicleState.PAUSED},
    VehicleState.ASSIGNED: {VehicleState.EN_ROUTE, VehicleState.DEGRADED, VehicleState.PAUSED, VehicleState.IDLE},
    VehicleState.EN_ROUTE: {VehicleState.OPERATING, VehicleState.DEGRADED, VehicleState.PAUSED},
    VehicleState.OPERATING: {VehicleState.RETURNING, VehicleState.DEGRADED, VehicleState.PAUSED},
    VehicleState.RETURNING: {VehicleState.IDLE, VehicleState.DEGRADED, VehicleState.PAUSED},
    VehicleState.DEGRADED: {VehicleState.REMOTE_ASSIST, VehicleState.SAFE_STOP, VehicleState.IDLE,
                            VehicleState.EN_ROUTE, VehicleState.OPERATING, VehicleState.RETURNING},
    VehicleState.PAUSED: {VehicleState.IDLE, VehicleState.EN_ROUTE, VehicleState.OPERATING, VehicleState.SAFE_STOP},
    VehicleState.REMOTE_ASSIST: {VehicleState.IDLE, VehicleState.EN_ROUTE, VehicleState.SAFE_STOP},
    VehicleState.SAFE_STOP: {VehicleState.IDLE},
}

# 任务状态转移表
TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.ASSIGNED},
    TaskStatus.ASSIGNED: {TaskStatus.IN_PROGRESS, TaskStatus.REASSIGNED, TaskStatus.FAILED},
    TaskStatus.IN_PROGRESS: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.REASSIGNED},
    TaskStatus.REASSIGNED: {TaskStatus.PENDING, TaskStatus.ASSIGNED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {TaskStatus.PENDING},
}


def transition_vehicle(current: VehicleState, target: VehicleState) -> VehicleState:
    allowed = VEHICLE_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransition(f"vehicle: {current} -> {target} not allowed")
    return target


def transition_task(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    allowed = TASK_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransition(f"task: {current} -> {target} not allowed")
    return target
