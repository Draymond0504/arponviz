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
from .state_machine import transition_task, transition_vehicle
from .storage import AlertORM, EventORM, RuntimeSnapshotORM, TaskORM, VehicleORM, get_session

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
        self.route_wait: dict[str, str] = {}
        self.closures: dict[str, dict] = {}
        self.suspended_state: dict[str, VehicleState] = {}
        self.revision = 0
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
        if vehicle.next_node_id:
            a, b = vehicle.node_id, vehicle.next_node_id
        elif vehicle.target_node_id and vehicle.node_id != vehicle.target_node_id:
            path = world.layout.shortest_path(vehicle.node_id, vehicle.target_node_id)
            a, b = path[:2]
        else:
            raise ValueError("车辆没有待行驶路段，请通过道路管理选择封闭路段")
        closure = close_road(world, a, b, "模拟道路阻塞")
        return Event(id=world.alerts[closure['alert_id']].event_id, timestamp=now,
                     source=f"road:{closure['id']}", event_type="route_blocked",
                     severity=EventSeverity.WARNING, payload=closure)
    else:
        raise ValueError(f"unknown anomaly_type: {anomaly_type}")
    if event:
        alert = _raise_alert(world, event)
        event.id = alert.event_id
    return event


def record_event(world: World, event_type: str, source: str, task_id=None, **payload) -> Event:
    event = Event(id=str(uuid.uuid4()), timestamp=world.now, source=source,
                  event_type=event_type, severity=EventSeverity.INFO,
                  related_task_id=task_id, payload=payload)
    world.pending_events.append(event)
    return event


def _raise_alert(world: World, event: Event) -> Alert:
    # Repeated observations of the same unresolved condition are idempotent.
    for existing in world.alerts.values():
        if existing.resolved_at is None and existing.anomaly_type == event.event_type and event.source in existing.impact_scope:
            return existing
    vehicle = world.vehicles.get(event.source)
    if vehicle:
        event.related_task_id = vehicle.current_task_id
    world.pending_events.append(event)
    alert = Alert(id=str(uuid.uuid4()), event_id=event.id, anomaly_type=event.event_type,
                  impact_scope=[event.source], suggested_action=anomaly.SUGGESTED_ACTION.get(event.event_type, "人工核实"))
    world.alerts[alert.id] = alert
    if vehicle:
        if vehicle.state not in (VehicleState.DEGRADED, VehicleState.SAFE_STOP):
            world.suspended_state[vehicle.id] = vehicle.state
            vehicle.state = transition_vehicle(vehicle.state, VehicleState.DEGRADED)
        vehicle.current_speed_mps = 0.0
        # Only uncollected work is transferable. Cargo/service already in progress
        # stays with its vehicle until recovery; no implicit physical handover.
        if event.severity == EventSeverity.CRITICAL and vehicle.current_task_id:
            task = world.tasks.get(vehicle.current_task_id)
            if task and task.status == TaskStatus.ASSIGNED and vehicle.task_phase == "to_origin":
                task.status = transition_task(task.status, TaskStatus.REASSIGNED)
                task.assigned_vehicle_id = None
                record_event(world, "task_requeued", vehicle.id, task.id, reason=event.event_type)
                vehicle.current_task_id = None
                vehicle.task_phase = "returning"
                vehicle.target_node_id = world.home_node[vehicle.id]
                vehicle.planned_path = []
                world.suspended_state[vehicle.id] = VehicleState.RETURNING
    return alert


def resolve_alert(world: World, alert_id: str, resolution: str) -> Alert:
    alert = world.alerts[alert_id]
    if alert.resolved_at is not None:
        return alert
    # Road alerts and actual closures always resolve together.
    for closure in world.closures.values():
        if closure['alert_id'] == alert_id and closure['reopened_at'] is None:
            reopen_road(world, closure['id'], resolution)
            return alert
    alert.resolution = resolution
    alert.resolved_at = world.now
    source = alert.impact_scope[0] if alert.impact_scope else 'operator'
    vehicle = world.vehicles.get(source)
    record_event(world, "alert_resolved", source, vehicle.current_task_id if vehicle else None,
                 alert_id=alert.id, resolution=resolution)
    remaining = any(a.resolved_at is None and source in a.impact_scope for a in world.alerts.values())
    if vehicle and vehicle.state == VehicleState.DEGRADED and not remaining:
        resume_state = world.suspended_state.pop(vehicle.id, VehicleState.IDLE)
        vehicle.state = transition_vehicle(vehicle.state, resume_state)
        vehicle.last_heartbeat = world.now
        vehicle.position_quality_streak_low = 0
        record_event(world, "vehicle_resumed", vehicle.id, vehicle.current_task_id,
                     state=resume_state.value, phase=vehicle.task_phase)
    return alert


def close_road(world: World, from_node: str, to_node: str, reason: str) -> dict:
    world.layout.edge_distance(from_node, to_node)  # Reject nonexistent links.
    key = tuple(sorted((from_node, to_node)))
    for closure in world.closures.values():
        if tuple(sorted((closure['from_node'], closure['to_node']))) == key and closure['reopened_at'] is None:
            return closure
    closure_id = str(uuid.uuid4())
    event = Event(id=str(uuid.uuid4()), timestamp=world.now, source=f"road:{closure_id}",
                  event_type="route_blocked", severity=EventSeverity.WARNING,
                  payload={"from_node": from_node, "to_node": to_node, "reason": reason})
    alert = _raise_alert(world, event)
    closure = dict(id=closure_id, from_node=from_node, to_node=to_node, reason=reason,
                   closed_at=world.now.isoformat(), reopened_at=None, resolution=None, alert_id=alert.id)
    world.closures[closure_id] = closure
    world.layout.blocked_edges.add(key)
    for vehicle in world.vehicles.values():
        vehicle.planned_path = []
        if vehicle.next_node_id and world.layout.is_blocked(vehicle.node_id, vehicle.next_node_id):
            vehicle.current_speed_mps = 0.0
    record_event(world, "road_closed", event.source, from_node=from_node, to_node=to_node, reason=reason)
    return closure


def reopen_road(world: World, closure_id: str, resolution: str) -> dict:
    closure = world.closures[closure_id]
    if closure['reopened_at'] is not None:
        return closure
    closure['reopened_at'] = world.now.isoformat()
    closure['resolution'] = resolution
    world.layout.blocked_edges.discard(tuple(sorted((closure['from_node'], closure['to_node']))))
    alert = world.alerts[closure['alert_id']]
    alert.resolved_at = world.now
    alert.resolution = resolution
    for vehicle in world.vehicles.values():
        vehicle.planned_path = []
    record_event(world, "road_reopened", f"road:{closure_id}", resolution=resolution)
    return closure


def _set_route_wait(world: World, vehicle: Vehicle, reason: str | None) -> None:
    previous = world.route_wait.get(vehicle.id)
    if reason:
        vehicle.current_speed_mps = 0.0
        world.route_wait[vehicle.id] = reason
        if previous != reason:
            record_event(world, "route_waiting", vehicle.id, vehicle.current_task_id, reason=reason)
    elif previous:
        world.route_wait.pop(vehicle.id, None)
        record_event(world, "route_resumed", vehicle.id, vehicle.current_task_id)


def _start_assignment(world: World, vehicle: Vehicle, task: Task, now: datetime) -> None:
    vehicle.state = transition_vehicle(vehicle.state, VehicleState.ASSIGNED)
    vehicle.current_task_id = task.id
    vehicle.task_phase = "to_origin"
    vehicle.target_node_id = task.origin
    task.status = transition_task(task.status, TaskStatus.ASSIGNED)
    task.assigned_vehicle_id = vehicle.id
    vehicle.state = transition_vehicle(vehicle.state, VehicleState.EN_ROUTE)
    record_event(world, "task_assigned", vehicle.id, task.id)


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
        if vehicle.node_id == vehicle.target_node_id and vehicle.next_node_id is None:
            vehicle.next_node_id = None
            vehicle.edge_progress_m = 0.0
            vehicle.current_speed_mps = 0.0
            vehicle.planned_path = []
            return True

        if vehicle.next_node_id is not None and layout.is_blocked(vehicle.node_id, vehicle.next_node_id):
            if vehicle.edge_progress_m <= 1e-9:
                vehicle.next_node_id = None
                vehicle.planned_path = []
            else:
                _set_route_wait(world, vehicle, "当前路段封闭，原地等待恢复")
                return False

        if vehicle.next_node_id is None:
            if (not vehicle.planned_path or vehicle.planned_path[0] != vehicle.node_id
                    or vehicle.planned_path[-1] != vehicle.target_node_id
                    or any(layout.is_blocked(a, b) for a, b in zip(vehicle.planned_path, vehicle.planned_path[1:]))):
                try:
                    vehicle.planned_path = layout.shortest_path(vehicle.node_id, vehicle.target_node_id)
                except ValueError:
                    _set_route_wait(world, vehicle, "无可通行路线，等待道路恢复")
                    return False
            vehicle.next_node_id = vehicle.planned_path[1]
            vehicle.edge_progress_m = 0.0
        elif not vehicle.planned_path:
            # A closure/repair can change the target mid-edge. Finish the current
            # open segment before replanning; retain the exact physical position.
            try:
                vehicle.planned_path = [vehicle.node_id] + layout.shortest_path(vehicle.next_node_id, vehicle.target_node_id)
            except ValueError:
                vehicle.planned_path = [vehicle.node_id, vehicle.next_node_id]
        _set_route_wait(world, vehicle, None)

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

    if vehicle.node_id == vehicle.target_node_id and vehicle.next_node_id is None and world.crossing_wait.get(vehicle.id, 0.0) <= 0:
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
            task.started_at = task.started_at or now
            record_event(world, "task_started", vehicle.id, task.id)
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
            record_event(world, "task_completed", vehicle.id, task.id)
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
            record_event(world, "vehicle_returned", vehicle.id)
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
    from .checkpoint import dump_world

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
        session.merge(RuntimeSnapshotORM(id=1, payload=dump_world(world)))
        session.commit()
        world.pending_events.clear()
    finally:
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
    world.revision += 1
    _persist(world)
