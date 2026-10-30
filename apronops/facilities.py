"""Finite off-carriageway depot bays and FIFO entrance holding pockets.

Coordinates remain graph coordinates: bays/pockets are logical resources,
not surveyed geometry. Reservation is acquired before traversing a depot spur.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import heapq
import math

from .models import VehicleState

STOPPED = {VehicleState.DEGRADED, VehicleState.SAFE_STOP, VehicleState.PAUSED, VehicleState.REMOTE_ASSIST}
DEFAULT = {'capacity': 2, 'pickup_s': 10.0, 'recovery_after_s': 30.0}


def initialize(world, config=None):
    world.facility_config = {n.id: dict(DEFAULT) for n in world.layout.nodes.values() if n.type == 'depot'}
    for nid, values in (config or {}).items():
        if nid not in world.facility_config or not isinstance(values, dict) or set(values)-set(DEFAULT):
            raise ValueError(f'invalid depot configuration: {nid}')
        merged = {**DEFAULT, **values}
        if type(merged['capacity']) is not int or merged['capacity'] < 1:
            raise ValueError('depot capacity must be a positive integer')
        for key in ('pickup_s', 'recovery_after_s'):
            if not isinstance(merged[key], (int,float)) or not math.isfinite(merged[key]) or merged[key] <= 0:
                raise ValueError(f'{key} must be finite and positive')
        world.facility_config[nid] = merged
    world.berths = {nid: {} for nid in world.facility_config}
    world.berth_reservations = {nid: {} for nid in world.facility_config}
    world.depot_queues = {nid: [] for nid in world.facility_config}
    world.pickup_remaining = {}
    world.recovery_detours = {}
    world.recovery_route_lock = {}
    world.stall_state = {}
    world.wait_for = {}
    world.deadlock_cycles = []
    world.recovery_counts = {'detected': 0, 'cycles': 0, 'rerouted': 0, 'relocations': 0, 'resolved': 0, 'unresolved': 0}
    world.depot_wait_s = 0.
    world.next_dispatch_at = None
    world.dispatch_stats = {'epochs': 0, 'search_nodes': 0, 'truncated_epochs': 0}
    for vehicle in world.vehicles.values():
        if vehicle.node_id in world.berths and vehicle.edge_progress_m <= 1e-9:
            slots = world.berths[vehicle.node_id]
            if len(slots) >= world.facility_config[vehicle.node_id]['capacity']:
                raise ValueError(f'initial vehicles exceed depot capacity: {vehicle.node_id}')
            slots[vehicle.id] = len(slots)


def _event(world, kind, vid, **payload):
    from .simulator import record_event
    record_event(world, kind, vid, world.vehicles[vid].current_task_id, **payload)


def parked(world, vehicle):
    return vehicle.edge_progress_m <= 1e-9 and vehicle.id in world.berths.get(vehicle.node_id, {})


def holding(world, vehicle):
    # Admission waits happen at the spur mouth, before any travel on that edge.
    return vehicle.edge_progress_m <= 1e-9 and any(
        q['vehicle_id'] == vehicle.id for queue in world.depot_queues.values() for q in queue)


def off_road(world, vehicle):
    return parked(world, vehicle) or holding(world, vehicle)


def free_slot(world, nid):
    used = set(world.berths[nid].values()) | set(world.berth_reservations[nid].values())
    return next((i for i in range(world.facility_config[nid]['capacity']) if i not in used), None)


def cleanup(world):
    for nid, queue in world.depot_queues.items():
        queue[:] = [q for q in queue if q['vehicle_id'] in world.vehicles
                    and world.vehicles[q['vehicle_id']].target_node_id == nid
                    and world.vehicles[q['vehicle_id']].state not in STOPPED]
        for vid in list(world.berth_reservations[nid]):
            v = world.vehicles.get(vid)
            # A vehicle already approaching retains its reservation during faults.
            if v is None or (v.target_node_id != nid and v.next_node_id != nid):
                del world.berth_reservations[nid][vid]
        for vid in list(world.berths[nid]):
            if vid not in world.vehicles:
                del world.berths[nid][vid]
    for vid in list(world.pickup_remaining):
        if vid not in world.vehicles or world.vehicles[vid].task_phase != 'to_origin':
            del world.pickup_remaining[vid]


def admit(world, vehicle, nid):
    if nid not in world.berths or vehicle.id in world.berths[nid] or vehicle.id in world.berth_reservations[nid]:
        return True
    queue = world.depot_queues[nid]
    if not any(q['vehicle_id'] == vehicle.id for q in queue):
        queue.append({'vehicle_id': vehicle.id, 'since': world.now.isoformat()})
        _event(world, 'depot_queued', vehicle.id, depot_id=nid)
    slot = free_slot(world, nid)
    if slot is not None and queue[0]['vehicle_id'] == vehicle.id:
        queue.pop(0)
        world.berth_reservations[nid][vehicle.id] = slot
        _event(world, 'depot_reserved', vehicle.id, depot_id=nid, berth=slot)
        return True
    return False


def arrive(world, vehicle):
    nid = vehicle.node_id
    if nid not in world.berths:
        return
    if vehicle.id not in world.berths[nid]:
        slot = world.berth_reservations[nid].pop(vehicle.id, None)
        if slot is None:
            slot = free_slot(world, nid)
        if slot is None:
            raise RuntimeError(f'depot capacity invariant failed at {nid}')
        world.berths[nid][vehicle.id] = slot
        _event(world, 'depot_entered', vehicle.id, depot_id=nid, berth=slot)


def depart(world, vehicle):
    slots = world.berths.get(vehicle.node_id, {})
    if vehicle.id in slots:
        slot = slots.pop(vehicle.id)
        _event(world, 'depot_departed', vehicle.id, depot_id=vehicle.node_id, berth=slot)


def pickup_ready(world, vehicle, dt):
    if vehicle.node_id not in world.berths:
        return True
    if vehicle.id not in world.pickup_remaining:
        world.pickup_remaining[vehicle.id] = world.facility_config[vehicle.node_id]['pickup_s']
        return False
    world.pickup_remaining[vehicle.id] -= dt
    if world.pickup_remaining[vehicle.id] <= 1e-8:
        del world.pickup_remaining[vehicle.id]
        return True
    return False


def _release_seconds(world, nid, vid):
    v = world.vehicles[vid]
    if v.state in STOPPED or holding(world,v) or (v.state == VehicleState.IDLE and v.current_task_id is None):
        return math.inf  # Never invent a known repair/departure time.
    if v.task_phase == 'servicing':
        return max(0., v.service_remaining_s) + .5
    if v.task_phase == 'to_origin' and v.node_id == nid:
        return max(0., world.pickup_remaining.get(vid, world.facility_config[nid]['pickup_s'])) + .5
    return .5


def forecast_wait(world, nid, arrival, vehicle_id=None):
    """Finite-server FIFO estimate from current occupancy; None means unknown.

Reservations include remaining approach time. Only observed queues are read;
no future scenario releases/fault repairs are visible to the estimator.
"""
    if nid not in world.berths or vehicle_id in world.berths[nid]:
        return 0.
    if vehicle_id in world.berth_reservations[nid]:
        return 0.
    slots = [0.] * world.facility_config[nid]['capacity']
    for vid, slot in world.berths[nid].items():
        slots[slot] = _release_seconds(world, nid, vid)
    for vid, slot in world.berth_reservations[nid].items():
        v = world.vehicles[vid]
        if v.state in STOPPED or v.task_phase == 'returning' or v.state == VehicleState.IDLE:
            slots[slot] = math.inf
        else:
            remaining = world.layout.edge_distance(v.node_id,nid)-v.edge_progress_m if v.next_node_id == nid else 0.
            slots[slot] = remaining/max(.1, min(v.speed_mps,world.layout.speed_limit(v.node_id,nid))) + world.facility_config[nid]['pickup_s']
    heapq.heapify(slots)
    for q in world.depot_queues[nid]:
        if q['vehicle_id'] == vehicle_id:
            break
        available = heapq.heappop(slots)
        queued = world.vehicles[q['vehicle_id']]
        duration = world.facility_config[nid]['pickup_s'] if queued.task_phase == 'to_origin' else math.inf
        heapq.heappush(slots, available+duration)
    seconds = max(0., (arrival-world.now).total_seconds())
    value = max(0., slots[0]-seconds)
    return value if math.isfinite(value) else None


def rows(world):
    result = []
    for nid, config in world.facility_config.items():
        wait = forecast_wait(world,nid,world.now)
        result.append({'id':nid, **config, 'occupied':len(world.berths[nid]),
            'reserved':len(world.berth_reservations[nid]), 'queue_length':len(world.depot_queues[nid]),
            'occupants':dict(world.berths[nid]), 'reservations':dict(world.berth_reservations[nid]),
            'queue':[dict(q) for q in world.depot_queues[nid]], 'predicted_wait_s':wait})
    return result


def recovery_action(world, vid):
    detour = world.recovery_detours.get(vid)
    return f"挪车至 {detour['to']}，释放 {detour['from']} 泊位" if detour else ''
