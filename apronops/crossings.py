"""Exclusive, synthetic aircraft/vehicle crossing interlock.

A radial graph zone extends along every incident vehicle edge. Its stop line is
min(3m, shortest incident edge / 3) from the crossing, so adjacent zones do not
overlap. Aircraft windows are requests, not uncontrollable measured occupancy:
actual entry is granted only after vehicles clear, preserving request duration.
"""
from __future__ import annotations

from datetime import timedelta

from .models import CrossingOccupancy


def zone_radius(layout, crossing_id):
    return min(3., min(d for _, d, _ in layout.adjacency[crossing_id]) / 3.)


def vehicle_in_zone(world, vehicle, crossing_id):
    radius = zone_radius(world.layout, crossing_id)
    if vehicle.node_id == crossing_id:
        return vehicle.next_node_id is None or vehicle.edge_progress_m < radius - 1e-8
    if vehicle.next_node_id == crossing_id:
        return world.layout.edge_distance(vehicle.node_id, crossing_id) - vehicle.edge_progress_m < radius - 1e-8
    return False


def register_occupancy(world, occupancy: CrossingOccupancy):
    from .simulator import record_event

    node = world.layout.nodes.get(occupancy.crossing_id)
    if node is None or node.type != 'crossing':
        raise ValueError('无效的交叉点')
    if occupancy.requested_end <= occupancy.requested_start or occupancy.predicted_end <= occupancy.predicted_start:
        raise ValueError('占用结束时间必须晚于开始时间')
    existing = world.occupancies.get(occupancy.id)
    if existing:
        keys = ('crossing_id', 'aircraft_id', 'predicted_start', 'predicted_end', 'requested_start', 'requested_end')
        if all(getattr(existing, k) == getattr(occupancy, k) for k in keys):
            return existing
        raise ValueError('占用记录 ID 已存在；请使用新的 ID')
    if occupancy.requested_start < world.now:
        raise ValueError('不能向过去插入占用请求')
    world.occupancies[occupancy.id] = occupancy
    record_event(world, 'crossing_requested', occupancy.aircraft_id,
                 occupancy=occupancy.model_dump(mode='json'))
    return occupancy


def update_crossings(world):
    from .simulator import record_event

    # Clear a vehicle token only after its position is outside the zone.
    for cid, vid in list(world.crossing_tokens.items()):
        if not vehicle_in_zone(world, world.vehicles[vid], cid):
            del world.crossing_tokens[cid]
    for request in sorted(world.occupancies.values(), key=lambda r: (r.requested_start, r.id)):
        if request.source != 'aircraft_motion' and request.status == 'active' and world.now >= request.actual_end:
            request.status = 'completed'
            record_event(world, 'crossing_aircraft_exited', request.aircraft_id, occupancy_id=request.id)
        if request.status not in ('scheduled', 'waiting_clearance') or request.requested_start > world.now:
            continue
        cid = request.crossing_id
        occupied = any(vehicle_in_zone(world, v, cid) for v in world.vehicles.values())
        other_aircraft = any(r.crossing_id == cid and r.status == 'active' for r in world.occupancies.values())
        if occupied or other_aircraft:
            if request.status != 'waiting_clearance':
                request.status = 'waiting_clearance'
                record_event(world, 'crossing_clearance_delayed', request.aircraft_id, occupancy_id=request.id)
            continue
        request.status = 'active'
        request.actual_start = world.now
        request.actual_end = world.now + (request.requested_end - request.requested_start)
        record_event(world, 'crossing_aircraft_entered', request.aircraft_id, occupancy_id=request.id,
                     delay_s=(request.actual_start - request.requested_start).total_seconds())


def admission_reason(world, vehicle, crossing_id, interval_end):
    if world.crossing_tokens.get(crossing_id) == vehicle.id or vehicle_in_zone(world, vehicle, crossing_id):
        return ''  # Already inside: allow egress even when an aircraft requests entry.
    for request in world.occupancies.values():
        if request.crossing_id != crossing_id or request.status == 'completed':
            continue
        if request.status in ('active', 'waiting_clearance') or request.requested_start < interval_end:
            return f'等待飞机通行：{request.aircraft_id} / {crossing_id}'
    if crossing_id in world.crossing_tokens or any(
            v.id != vehicle.id and vehicle_in_zone(world, v, crossing_id) for v in world.vehicles.values()):
        return f'等待交叉区车辆清空：{crossing_id}'
    return ''


def crossing_rows(world):
    rows = []
    for node in world.layout.nodes.values():
        if node.type != 'crossing':
            continue
        active = [r.aircraft_id for r in world.occupancies.values() if r.crossing_id == node.id and r.status == 'active']
        pending = [r.aircraft_id for r in world.occupancies.values() if r.crossing_id == node.id and r.status == 'waiting_clearance']
        vehicles = [v.id for v in world.vehicles.values() if vehicle_in_zone(world, v, node.id)]
        rows.append({'id': node.id, 'x': node.x, 'y': node.y, 'radius_m': zone_radius(world.layout, node.id),
                     'aircraft': active, 'waiting_aircraft': pending, 'vehicles': vehicles,
                     'status': '飞机占用' if active else '等待清空' if pending else '车辆通行' if vehicles else '开放'})
    return rows
