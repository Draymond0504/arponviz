"""单车运动学与局部反应式避障：加减速限制、跟车安全速度、转弯限速。

不做整体调度/多车协同优化，只服务单车的感知-决策-执行闭环。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .layout import Layout
from .models import Vehicle, VehicleType

if TYPE_CHECKING:
    from .simulator import World

PHYSICS_SUBSTEP_S = 0.5  # 物理子步长，用于加减速积分

# 按车辆类型查表的加减速上限（m/s^2），示意值，非真实机场安全标准
ACCEL_MPS2 = {
    VehicleType.TRACTOR: 0.8,
    VehicleType.SHUTTLE: 1.0,
    VehicleType.CATERING: 0.6,
    VehicleType.AMR_INSPECT: 0.5,
}
DECEL_MPS2 = {
    VehicleType.TRACTOR: 1.2,
    VehicleType.SHUTTLE: 1.5,
    VehicleType.CATERING: 1.0,
    VehicleType.AMR_INSPECT: 0.8,
}

SAFETY_GAP_M = 5.0  # 车间安全净空（含车长），示意值
NODE_APPROACH_RADIUS_M = 15.0  # 临近转弯节点多少米内开始限速
NODE_APPROACH_SPEED_MPS = 1.5  # 转弯/进站限速
TURN_ANGLE_THRESHOLD_DEG = 30.0  # 超过该转角视为"转弯"，否则算直行


def accel_decel_for(vehicle_type: VehicleType) -> tuple[float, float]:
    return ACCEL_MPS2[vehicle_type], DECEL_MPS2[vehicle_type]


def safe_following_speed(gap_m: float, decel_mps2: float, max_speed_mps: float) -> float:
    """当前净空下，按 decel_mps2 减速仍能刹停的最高速度。"""
    gap = max(gap_m, 0.0)
    return min(max_speed_mps, math.sqrt(2 * decel_mps2 * gap))


def sense_ahead_gap(world: "World", vehicle: Vehicle) -> float | None:
    """只看车辆当前所在边（node_id -> next_node_id），返回前方净空距离；无相关车辆时返回 None。"""
    if vehicle.next_node_id is None:
        return None
    layout = world.layout
    gaps: list[float] = []
    for other in world.vehicles.values():
        if other.id == vehicle.id:
            continue
        # 对头：对方从本车的目标节点驶来，且终点正是本车所在节点
        if other.node_id == vehicle.next_node_id and other.next_node_id == vehicle.node_id:
            # The crossing owner must be able to clear the zone; applying the
            # old lexical rule here can deadlock it against a car at the stop line.
            owner = world.crossing_tokens.get(vehicle.node_id) or world.crossing_tokens.get(vehicle.next_node_id)
            if owner == other.id or (owner != vehicle.id and vehicle.id > other.id):
                gaps.append(0.0)  # id 字典序大的一方让行，避免死锁
            continue
        # 同边同向，且在本车前方
        if other.node_id == vehicle.node_id and other.next_node_id == vehicle.next_node_id:
            if other.edge_progress_m >= vehicle.edge_progress_m:
                gaps.append(max(0.0, other.edge_progress_m - vehicle.edge_progress_m - SAFETY_GAP_M))
            continue
        # 对方已停在本车目标节点（占位，尚未驶向下一段）
        if other.node_id == vehicle.next_node_id and other.next_node_id is None:
            edge_len = layout.edge_distance(vehicle.node_id, vehicle.next_node_id)
            gaps.append(max(0.0, edge_len - vehicle.edge_progress_m - SAFETY_GAP_M))
    return min(gaps) if gaps else None


def _angle_between_deg(v1: tuple[float, float], v2: tuple[float, float]) -> float:
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    cross = v1[0] * v2[1] - v1[1] * v2[0]
    return math.degrees(abs(math.atan2(cross, dot)))


def turn_slowdown_speed(layout: Layout, planned_path: list[str], remaining_to_next_node_m: float) -> float | None:
    """临近转弯节点时的限速；直行路段或最后一段（无后续转弯）返回 None（不限速）。

    要求 planned_path[0] 是车辆当前所在节点、planned_path[1] 是下一个节点。
    """
    if len(planned_path) < 3:
        return None
    if remaining_to_next_node_m > NODE_APPROACH_RADIUS_M:
        return None
    a = layout.node_xy(planned_path[0])
    b = layout.node_xy(planned_path[1])
    c = layout.node_xy(planned_path[2])
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    if _angle_between_deg(v1, v2) >= TURN_ANGLE_THRESHOLD_DEG:
        return NODE_APPROACH_SPEED_MPS
    return None
