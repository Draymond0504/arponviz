"""Conservative liveness recovery: depot relocation and alternate open routes.

No teleportation, reversing on occupied edges, fault auto-clear, or preemption
of collected cargo. Unrecoverable stalls remain visible for manual action.
"""
from __future__ import annotations

from datetime import datetime

from . import facilities
from .models import VehicleState

STALL_S = 30.


def dependency_cycles(graph):
    """Return strongly connected wait components, not ordinary one-way queues."""
    index, stack, on_stack, indices, low, components = 0, [], set(), {}, {}, []
    def visit(node):
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node); on_stack.add(node)
        for other in graph.get(node, []):
            if other not in indices:
                visit(other); low[node] = min(low[node],low[other])
            elif other in on_stack:
                low[node] = min(low[node],indices[other])
        if low[node] == indices[node]:
            group = []
            while True:
                other = stack.pop(); on_stack.remove(other); group.append(other)
                if other == node: break
            if len(group) > 1:
                components.append(sorted(group))
    for node in sorted(graph):
        if node not in indices: visit(node)
    return sorted(components)


def _event(world, kind, vid, **payload):
    from .simulator import record_event
    record_event(world,kind,vid,world.vehicles[vid].current_task_id,**payload)


def _relocate(world, nid):
    queued_targets = {n for n,q in world.depot_queues.items() if q}
    candidates = []
    for vid in world.berths[nid]:
        v = world.vehicles[vid]
        if v.state in facilities.STOPPED or v.edge_progress_m > 1e-9 or vid in world.recovery_detours:
            continue
        # Idle residents and vehicles waiting before departure can move; service
        # or pickup handling already in progress is never interrupted.
        if v.task_phase == 'servicing' or vid in world.pickup_remaining:
            continue
        for dest in world.berths:
            if dest == nid or dest in queued_targets or facilities.free_slot(world,dest) is None:
                continue
            try:
                path = world.layout.shortest_path(nid,dest)
                distance = world.layout.path_distance(nid,dest)
            except ValueError:
                continue
            candidates.append((v.state != VehicleState.IDLE, distance, vid, dest, path))
    if not candidates:
        return None
    _, _, vid, dest, path = min(candidates)
    v = world.vehicles[vid]
    world.recovery_detours[vid] = {'target':v.target_node_id, 'state':v.state.value, 'from':nid, 'to':dest}
    world.berth_reservations[dest][vid] = facilities.free_slot(world,dest)
    v.target_node_id = dest
    v.next_node_id = None
    v.edge_progress_m = 0.
    v.planned_path = path
    # Detour is orthogonal to task state and is advanced before normal phases.
    _event(world,'deadlock_relocation_started',vid,from_depot=nid,to_depot=dest)
    world.recovery_counts['relocations'] += 1
    facilities.cleanup(world)
    return vid


def _reroute(world, v):
    if not v.target_node_id or v.next_node_id is None or v.edge_progress_m > 1e-9:
        return False
    # Only choose another path while still exactly at a node; never jump back.
    key = tuple(sorted((v.node_id,v.next_node_id)))
    was_blocked = key in world.layout.blocked_edges
    world.layout.blocked_edges.add(key)
    try:
        path = world.layout.shortest_path(v.node_id,v.target_node_id)
    except ValueError:
        return False
    finally:
        if not was_blocked:
            world.layout.blocked_edges.discard(key)
    v.planned_path = path
    v.next_node_id = None
    v.edge_progress_m = 0.
    world.recovery_route_lock[v.id] = True
    world.recovery_counts['rerouted'] += 1
    _event(world,'deadlock_rerouted',v.id,path=path)
    return True


def update(world, dt):
    facilities.cleanup(world)
    queue_by_vehicle = {q['vehicle_id']:(nid,q) for nid,queue in world.depot_queues.items() for q in queue}
    world.wait_for = {}
    for vid,(nid,_) in queue_by_vehicle.items():
        blockers = set(world.berths[nid]) | set(world.berth_reservations[nid])
        for item in world.depot_queues[nid]:
            if item['vehicle_id'] == vid: break
            blockers.add(item['vehicle_id'])
        world.wait_for[vid] = sorted(blockers-{vid})
    cycles = dependency_cycles(world.wait_for)
    for cycle in cycles:
        if cycle not in world.deadlock_cycles:
            world.recovery_counts['cycles'] += 1
            _event(world,'deadlock_cycle_detected',cycle[0],vehicles=cycle)
    world.deadlock_cycles = cycles
    for v in world.vehicles.values():
        if v.state in facilities.STOPPED:
            # Pausing the blocked vehicle is not evidence that the blockage
            # cleared. Preserve its incident and position until it can resume.
            continue
        reason = world.route_wait.get(v.id,'')
        monitored = v.state not in facilities.STOPPED and (v.id in queue_by_vehicle or '前方车辆' in reason)
        if v.id in queue_by_vehicle and v.state not in facilities.STOPPED:
            world.depot_wait_s += dt
            nid, _ = queue_by_vehicle[v.id]
            # A busy bay with a known finite completion is an ordinary queue.
            monitored = facilities.forecast_wait(world,nid,world.now,v.id) is None
        previous = world.stall_state.get(v.id)
        if not monitored or (previous and (abs(v.x-previous['x'])+abs(v.y-previous['y']) > .01)):
            if previous and previous['reported']:
                world.recovery_counts['resolved'] += 1
                _event(world,'deadlock_cleared',v.id)
            world.stall_state.pop(v.id,None)
            if not monitored:
                continue
            previous = None
        if previous is None:
            world.stall_state[v.id] = {'since':world.now.isoformat(), 'x':v.x,'y':v.y,
                                       'reported':False,'last_attempt':None}
            continue
        threshold = world.facility_config[queue_by_vehicle[v.id][0]]['recovery_after_s'] if v.id in queue_by_vehicle else STALL_S
        if (world.now-datetime.fromisoformat(previous['since'])).total_seconds() < threshold:
            continue
        if not previous['reported']:
            previous['reported'] = True
            world.recovery_counts['detected'] += 1
            _event(world,'deadlock_detected',v.id,reason=reason,threshold_s=threshold)
        if previous['last_attempt'] and (world.now-datetime.fromisoformat(previous['last_attempt'])).total_seconds() < threshold:
            continue
        previous['last_attempt'] = world.now.isoformat()
        if v.id in queue_by_vehicle:
            nid, _ = queue_by_vehicle[v.id]
            # Normal finite service queues are not deadlocked. Only unknown
            # release (idle/fault occupants) or a departure cycle is recovered.
            if facilities.forecast_wait(world,nid,world.now,v.id) is None:
                if not any(d['from'] == nid for d in world.recovery_detours.values()):
                    moved = _relocate(world,nid)
                    if moved:
                        continue
        elif _reroute(world,v):
            continue
        _event(world,'deadlock_unresolved',v.id,reason='无可用自动恢复动作；保留位置、任务和故障，等待现场处置')
    world.recovery_counts['unresolved'] = sum(s['reported'] for s in world.stall_state.values())
