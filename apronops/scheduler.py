"""规则调度器：优先级 + 最近可用车辆 / 截止时间紧迫度两种可插拔策略。"""
from __future__ import annotations

from typing import Callable, Optional

from .layout import Layout
from .models import Task, Vehicle, VehicleState

# 任务类型 -> 可执行车辆类型
TASK_VEHICLE_COMPAT = {
    "baggage_tow": {"tractor"},
    "catering_supply": {"catering"},
    "inspection": {"amr_inspect", "shuttle"},
}


def _compatible_idle_vehicles(task: Task, vehicles: list[Vehicle]) -> list[Vehicle]:
    allowed_types = TASK_VEHICLE_COMPAT.get(task.type.value, set())
    return [v for v in vehicles if v.type.value in allowed_types and v.state == VehicleState.IDLE
            and v.current_task_id is None and v.next_node_id is None and v.target_node_id is None]


def assign_nearest(task: Task, vehicles: list[Vehicle], layout: Layout) -> Optional[Vehicle]:
    """策略一：同类型空闲车辆中，按 (优先级降序不适用于单任务, 距任务起点最近) 选车。"""
    candidates = _compatible_idle_vehicles(task, vehicles)
    try:
        layout.shortest_path(task.origin, task.destination)
    except ValueError:
        return None
    if not candidates:
        return None
    best = None
    best_dist = float("inf")
    for v in candidates:
        try:
            d = layout.path_distance(v.node_id, task.origin)
        except ValueError:
            continue
        if d < best_dist:
            best_dist = d
            best = v
    return best


def assign_deadline_urgency(task: Task, vehicles: list[Vehicle], layout: Layout, now) -> Optional[Vehicle]:
    """策略二：优先满足截止时间最紧迫的任务，其余同策略一按最近距离选车。"""
    return assign_nearest(task, vehicles, layout)


def task_wait_reason(task: Task, tasks: dict[str, Task], now) -> str:
    if task.status.value not in ("PENDING", "REASSIGNED"):
        return ""
    if task.release_at is not None and (now is None or now < task.release_at):
        return "等待任务释放时间"
    if any(dep not in tasks or tasks[dep].status.value != "COMPLETED" for dep in task.depends_on):
        return "等待前置任务完成"
    return ""


def pick_task_order(tasks: list[Task], strategy: str = "priority", now=None) -> list[Task]:
    pending = [t for t in tasks if t.status.value in ("PENDING", "REASSIGNED")]
    by_id = {t.id: t for t in tasks}
    pending = [t for t in pending if not task_wait_reason(t, by_id, now)]
    if strategy == "deadline":
        return sorted(pending, key=lambda t: (t.deadline or t.created_at))
    return sorted(pending, key=lambda t: (-t.priority, t.created_at))


Strategy = Callable[[Task, list[Vehicle], Layout], Optional[Vehicle]]

STRATEGIES: dict[str, Strategy] = {
    "nearest": assign_nearest,
}
