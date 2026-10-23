"""事件驱动模拟循环：车辆移动、任务推进、异常检测、重分配、落库。"""
from __future__ import annotations

import random
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import anomaly, kinematics, scheduler
from .layout import Layout, load_layout
from .models import (
    Alert,
    Event,
    EventSeverity,
    Task,
    TaskStatus,
    TaskType,
    Vehicle,
    VehicleState,
    VehicleType,
)
from .state_machine import InvalidTransition, transition_task, transition_vehicle
from .storage import AlertORM, EventORM, TaskORM, VehicleORM, get_session

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 保障作业标准时长（秒），对应蓝图"保障作业标准时长"数据类别，MVP 用固定值模拟
SERVICE_DURATION_S = {
    TaskType.BAGGAGE_TOW.value: 8 * 60,
    TaskType.CATERING_SUPPLY.value: 10 * 60,
    TaskType.INSPECTION.value: 5 * 60,
}
BATTERY_DRAIN_PER_M = 0.02  # 每米耗电百分比（模拟线性耗电曲线）
TRACK_MAXLEN = 80  # 每辆车保留的轨迹点数（内存环形缓冲，不落库）


class World:
    def __init__(self, layout: Layout, vehicles: dict[str, Vehicle], tasks: dict[str, Task]):
        self.layout = layout
        self.vehicles = vehicles
        self.tasks = tasks
        self.alerts: dict[str, Alert] = {}
        self.pending_events: list[Event] = []
        self.crossing_wait: dict[str, float] = {}
        self.now = datetime.utcnow()
        self.home_node: dict[str, str] = {v.id: v.node_id for v in vehicles.values()}
        # 预填一个初始点，避免后台线程第一次 tick 前 /tracks 返回空列表
        self.tracks: dict[str, deque[tuple[datetime, float, float]]] = {
            v.id: deque([(self.now, v.x, v.y)], maxlen=TRACK_MAXLEN) for v in vehicles.values()
        }


def load_world(data_dir: Path | str = DATA_DIR) -> World:
    data_dir = Path(data_dir)
    layout = load_layout(str(data_dir / "apron_layout.yaml"))

    with open(data_dir / "vehicles_seed.yaml", "r", encoding="utf-8") as f:
        vraw = yaml.safe_load(f)["vehicles"]
    now = datetime.utcnow()
    vehicles: dict[str, Vehicle] = {}
    for v in vraw:
        x, y = layout.node_xy(v["home_node"])
        vehicles[v["id"]] = Vehicle(
            id=v["id"],
            type=VehicleType(v["type"]),
            node_id=v["home_node"],
            x=x,
            y=y,
            speed_mps=v["speed_mps"],
            battery=v.get("battery", 100.0),
            last_heartbeat=now,
        )

    with open(data_dir / "tasks_seed.yaml", "r", encoding="utf-8") as f:
        traw = yaml.safe_load(f)["tasks"]
    tasks: dict[str, Task] = {}
    for t in traw:
        tasks[t["id"]] = Task(
            id=t["id"],
            type=TaskType(t["type"]),
            origin=t["origin"],
            destination=t["destination"],
            priority=t.get("priority", 3),
            deadline=now + timedelta(seconds=t.get("deadline_offset_s", 1800)),
            created_at=now,
        )

    return World(layout, vehicles, tasks)


def inject_anomaly(world: World, anomaly_type: str, vehicle_id: str) -> Event:
    vehicle = world.vehicles[vehicle_id]
    now = world.now
    if anomaly_type == "communication_lost":
        vehicle.last_heartbeat = now - timedelta(seconds=anomaly.HEARTBEAT_PERIOD_S * (anomaly.HEARTBEAT_MISS_THRESHOLD + 1))
        event = anomaly.check_heartbeat(vehicle, now)
    elif anomaly_type == "position_quality_degraded":
        vehicle.position_quality_streak_low = anomaly.POSITION_QUALITY_STREAK_THRESHOLD - 1
        event = anomaly.check_position_quality(vehicle, True, now)
    elif anomaly_type == "route_blocked":
        event = anomaly.check_route_block(vehicle, base_travel_s=60, eta_s=200, now=now)
    else:
        raise ValueError(f"unknown anomaly_type: {anomaly_type}")
    if event:
        _raise_alert(world, event)
    return event


def _raise_alert(world: World, event: Event) -> Alert:
    world.pending_events.append(event)
    alert = Alert(
        id=str(uuid.uuid4()),
        event_id=event.id,
        anomaly_type=event.event_type,
        impact_scope=[event.source],
        suggested_action=anomaly.SUGGESTED_ACTION.get(event.event_type, "人工核实"),
    )
    world.alerts[alert.id] = alert
    vehicle = world.vehicles.get(event.source)
    if vehicle and vehicle.state not in (VehicleState.DEGRADED, VehicleState.SAFE_STOP):
        try:
            vehicle.state = transition_vehicle(vehicle.state, VehicleState.DEGRADED)
        except InvalidTransition:
            pass
        if event.severity == EventSeverity.CRITICAL and vehicle.current_task_id:
            task = world.tasks.get(vehicle.current_task_id)
            if task and task.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
                task.status = transition_task(task.status, TaskStatus.REASSIGNED)
                task.assigned_vehicle_id = None
                vehicle.current_task_id = None
                vehicle.task_phase = None
                vehicle.target_node_id = None
    return alert


def resolve_alert(world: World, alert_id: str, resolution: str) -> Alert:
    alert = world.alerts[alert_id]
    alert.resolution = resolution
    alert.resolved_at = world.now
    vehicle = world.vehicles.get(alert.impact_scope[0]) if alert.impact_scope else None
    if vehicle and vehicle.state == VehicleState.DEGRADED:
        vehicle.state = transition_vehicle(vehicle.state, VehicleState.IDLE)
        vehicle.last_heartbeat = world.now
        vehicle.position_quality_streak_low = 0
        if world.tasks.get(vehicle.current_task_id or "") is None:
            vehicle.current_task_id = None
    return alert


def _start_assignment(world: World, vehicle: Vehicle, task: Task, now: datetime) -> None:
    vehicle.state = transition_vehicle(vehicle.state, VehicleState.ASSIGNED)
    vehicle.current_task_id = task.id
    vehicle.task_phase = "to_origin"
    vehicle.target_node_id = task.origin
    task.status = transition_task(task.status, TaskStatus.ASSIGNED)
    task.assigned_vehicle_id = vehicle.id
    vehicle.state = transition_vehicle(vehicle.state, VehicleState.EN_ROUTE)


def _move_along_path(world: "World", vehicle: Vehicle, dt: float) -> bool:
    """单车运动学推进：加减速上限 + 局部反应式避障（跟车/对头让行/转弯限速）。

    只感知当前边上的其他车辆，不做整体路径规划或多车协同——单体智能闭环。
    """
    layout = world.layout
    remaining_dt = dt
    while remaining_dt > 1e-9:
        wait = world.crossing_wait.get(vehicle.id, 0.0)
        if wait > 0:
            used = min(wait, remaining_dt)
            world.crossing_wait[vehicle.id] = wait - used
            remaining_dt -= used
            vehicle.current_speed_mps = 0.0
            continue
        if vehicle.node_id == vehicle.target_node_id:
            vehicle.next_node_id = None
            vehicle.edge_progress_m = 0.0
            vehicle.current_speed_mps = 0.0
            vehicle.planned_path = []
            return True

        if vehicle.next_node_id is None:
            if not vehicle.planned_path or vehicle.planned_path[0] != vehicle.node_id or vehicle.planned_path[-1] != vehicle.target_node_id:
                vehicle.planned_path = layout.shortest_path(vehicle.node_id, vehicle.target_node_id)
            vehicle.next_node_id = vehicle.planned_path[1]
            vehicle.edge_progress_m = 0.0

        step = min(remaining_dt, kinematics.PHYSICS_SUBSTEP_S)
        remaining_dt -= step

        edge_len = layout.edge_distance(vehicle.node_id, vehicle.next_node_id)
        accel, decel = kinematics.accel_decel_for(vehicle.type)
        remaining_to_next_node = edge_len - vehicle.edge_progress_m

        desired_speed = min(vehicle.speed_mps, layout.speed_limit(vehicle.node_id, vehicle.next_node_id))
        gap = kinematics.sense_ahead_gap(world, vehicle)
        if gap is not None:
            desired_speed = min(desired_speed, kinematics.safe_following_speed(gap, decel, vehicle.speed_mps))
        turn_cap = kinematics.turn_slowdown_speed(layout, vehicle.planned_path, remaining_to_next_node)
        if turn_cap is not None:
            desired_speed = min(desired_speed, turn_cap)
        if layout.nodes[vehicle.next_node_id].type == "crossing":
            # Demo stop at the crossing vertex; no aircraft-clearance signal exists.
            desired_speed = min(desired_speed, max(0.3, (2 * decel * remaining_to_next_node) ** 0.5))

        if desired_speed > vehicle.current_speed_mps:
            new_speed = min(desired_speed, vehicle.current_speed_mps + accel * step)
        else:
            new_speed = max(desired_speed, vehicle.current_speed_mps - decel * step)
        new_speed = max(0.0, min(new_speed, layout.speed_limit(vehicle.node_id, vehicle.next_node_id)))

        travel = 0.5 * (vehicle.current_speed_mps + new_speed) * step
        travel = min(travel, remaining_to_next_node)
        travel = max(travel, 0.0)

        vehicle.current_speed_mps = new_speed
        vehicle.edge_progress_m += travel
        vehicle.battery = max(0.0, vehicle.battery - travel * BATTERY_DRAIN_PER_M)
        ax, ay = layout.node_xy(vehicle.node_id)
        bx, by = layout.node_xy(vehicle.next_node_id)
        t = vehicle.edge_progress_m / edge_len if edge_len else 1.0
        vehicle.x = ax + (bx - ax) * t
        vehicle.y = ay + (by - ay) * t

        if vehicle.edge_progress_m >= edge_len - 1e-6:
            vehicle.node_id = vehicle.next_node_id
            vehicle.next_node_id = None
            vehicle.edge_progress_m = 0.0
            if len(vehicle.planned_path) > 1:
                vehicle.planned_path = vehicle.planned_path[1:]
            if layout.nodes[vehicle.node_id].type == "crossing":
                world.crossing_wait[vehicle.id] = layout.metadata.get("crossing_wait_s", 3.0)
                vehicle.current_speed_mps = 0.0

        if new_speed <= 1e-9 and travel <= 1e-9:
            # 被让行/跟车逼停在原地，本次 tick 不再继续推进子步，避免死循环消耗 remaining_dt
            break

    if vehicle.node_id == vehicle.target_node_id and world.crossing_wait.get(vehicle.id, 0.0) <= 0:
        # 到达目标节点这一子步恰好耗尽 remaining_dt 时，循环会在触发顶部重置逻辑前退出，
        # 这里兜底清零速度/路径缓存，避免看板残留最后一段的行驶速度。
        vehicle.next_node_id = None
        vehicle.edge_progress_m = 0.0
        vehicle.current_speed_mps = 0.0
        vehicle.planned_path = []
        return True
    return False


def _advance_vehicle(world: World, vehicle: Vehicle, dt: float, now: datetime) -> None:
    if vehicle.state in (VehicleState.DEGRADED, VehicleState.SAFE_STOP, VehicleState.PAUSED, VehicleState.REMOTE_ASSIST):
        vehicle.current_speed_mps = 0.0
        return
    vehicle.last_heartbeat = now

    if vehicle.state == VehicleState.EN_ROUTE and vehicle.task_phase == "to_origin":
        if _move_along_path(world, vehicle, dt):
            task = world.tasks[vehicle.current_task_id]
            vehicle.state = transition_vehicle(vehicle.state, VehicleState.OPERATING)
            task.status = transition_task(task.status, TaskStatus.IN_PROGRESS)
            task.started_at = now
            vehicle.task_phase = "to_destination"
            vehicle.target_node_id = task.destination
        return

    if vehicle.state == VehicleState.OPERATING and vehicle.task_phase == "to_destination":
        if _move_along_path(world, vehicle, dt):
            task = world.tasks[vehicle.current_task_id]
            vehicle.task_phase = "servicing"
            vehicle.service_remaining_s = SERVICE_DURATION_S.get(task.type.value, 300)
        return

    if vehicle.state == VehicleState.OPERATING and vehicle.task_phase == "servicing":
        vehicle.service_remaining_s -= dt
        if vehicle.service_remaining_s <= 0:
            task = world.tasks[vehicle.current_task_id]
            task.status = transition_task(task.status, TaskStatus.COMPLETED)
            task.completed_at = now
            vehicle.state = transition_vehicle(vehicle.state, VehicleState.RETURNING)
            vehicle.task_phase = "returning"
            vehicle.target_node_id = world.home_node[vehicle.id]
            vehicle.current_task_id = None
        return

    if vehicle.state == VehicleState.RETURNING and vehicle.task_phase == "returning":
        if _move_along_path(world, vehicle, dt):
            vehicle.state = transition_vehicle(vehicle.state, VehicleState.IDLE)
            vehicle.task_phase = None
            vehicle.target_node_id = None
        return


def _run_anomaly_checks(world: World, now: datetime, random_inject_prob: float = 0.0) -> None:
    already_flagged_states = (VehicleState.DEGRADED, VehicleState.PAUSED, VehicleState.SAFE_STOP, VehicleState.REMOTE_ASSIST)
    for vehicle in world.vehicles.values():
        if vehicle.state in already_flagged_states:
            continue
        event = anomaly.check_heartbeat(vehicle, now)
        if event:
            _raise_alert(world, event)
            continue
        if random_inject_prob and random.random() < random_inject_prob and vehicle.current_task_id:
            required = vehicle.speed_mps * 60 * BATTERY_DRAIN_PER_M
            event = anomaly.check_battery(vehicle, required, now)
            if event:
                _raise_alert(world, event)


def _persist(world: World) -> None:
    session = get_session()
    try:
        for v in world.vehicles.values():
            session.merge(VehicleORM(**v.model_dump(exclude={"type", "state", "planned_path"}), type=v.type.value, state=v.state.value))
        for t in world.tasks.values():
            session.merge(TaskORM(**t.model_dump(exclude={"type", "status"}), type=t.type.value, status=t.status.value))
        for e in world.pending_events:
            session.merge(EventORM(**e.model_dump(exclude={"severity"}), severity=e.severity.value))
        for a in world.alerts.values():
            session.merge(AlertORM(**a.model_dump()))
        session.commit()
    finally:
        world.pending_events.clear()
        session.close()


def tick(world: World, dt: float = 5.0, random_anomaly_prob: float = 0.0) -> None:
    world.now = world.now + timedelta(seconds=dt)
    now = world.now

    for task in scheduler.pick_task_order(list(world.tasks.values())):
        vehicle = scheduler.assign_nearest(task, list(world.vehicles.values()), world.layout)
        if vehicle:
            _start_assignment(world, vehicle, task, now)

    for vehicle in world.vehicles.values():
        _advance_vehicle(world, vehicle, dt, now)
        # 异常态车辆位置不变，会在原地重复打点，属预期行为（等价于停在原地）
        world.tracks.setdefault(vehicle.id, deque(maxlen=TRACK_MAXLEN)).append((now, vehicle.x, vehicle.y))

    _run_anomaly_checks(world, now, random_anomaly_prob)
    _persist(world)
