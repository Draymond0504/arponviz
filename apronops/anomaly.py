"""异常检测规则（对应蓝图 §7 异常表），均为可调参数的模拟阈值。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Event, EventSeverity, Vehicle

HEARTBEAT_PERIOD_S = 10
HEARTBEAT_MISS_THRESHOLD = 3  # 超过 3 个心跳周期无上报 -> 通信丢失
POSITION_QUALITY_STREAK_THRESHOLD = 3  # 连续 N 次定位质量低于阈值
ROUTE_BLOCK_ETA_MULTIPLIER = 2.0  # 预计通行时间超过基准的倍数视为阻塞
BATTERY_SAFETY_MARGIN_PCT = 15.0  # 完成任务后需保留的最低电量


def _new_event(source: str, event_type: str, severity: EventSeverity, now: datetime, **payload) -> Event:
    return Event(
        id=str(uuid.uuid4()),
        timestamp=now,
        source=source,
        event_type=event_type,
        severity=severity,
        payload=payload,
    )


def check_heartbeat(vehicle: Vehicle, now: datetime) -> Event | None:
    gap = (now - vehicle.last_heartbeat).total_seconds()
    if gap > HEARTBEAT_PERIOD_S * HEARTBEAT_MISS_THRESHOLD:
        return _new_event(
            vehicle.id, "communication_lost", EventSeverity.CRITICAL, now,
            gap_seconds=gap,
        )
    return None


def check_position_quality(vehicle: Vehicle, quality_low: bool, now: datetime) -> Event | None:
    if quality_low:
        vehicle.position_quality_streak_low += 1
    else:
        vehicle.position_quality_streak_low = 0
    if vehicle.position_quality_streak_low >= POSITION_QUALITY_STREAK_THRESHOLD:
        return _new_event(
            vehicle.id, "position_quality_degraded", EventSeverity.WARNING, now,
            streak=vehicle.position_quality_streak_low,
        )
    return None


def check_route_block(vehicle: Vehicle, eta_s: float, base_travel_s: float, now: datetime) -> Event | None:
    if base_travel_s > 0 and eta_s > base_travel_s * ROUTE_BLOCK_ETA_MULTIPLIER:
        return _new_event(
            vehicle.id, "route_blocked", EventSeverity.WARNING, now,
            eta_s=eta_s, base_travel_s=base_travel_s,
        )
    return None


def check_battery(vehicle: Vehicle, required_pct: float, now: datetime) -> Event | None:
    if vehicle.battery - required_pct < BATTERY_SAFETY_MARGIN_PCT:
        return _new_event(
            vehicle.id, "battery_insufficient", EventSeverity.WARNING, now,
            battery=vehicle.battery, required_pct=required_pct,
        )
    return None


SUGGESTED_ACTION = {
    "communication_lost": "停止车辆；未取货任务重新排队，已取货任务等待本车恢复",
    "position_quality_degraded": "暂停作业；定位恢复后按原任务阶段续跑",
    "route_blocked": "未驶入车辆绕行，已驶入或无路可走的车辆等待；恢复道路后继续",
    "battery_insufficient": "禁止接新任务，安排回充或转派",
}
