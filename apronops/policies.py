"""Interchangeable rule policies; forecasts never read future requested windows."""
from __future__ import annotations

import heapq
from datetime import timedelta
from time import perf_counter

from . import scheduler

POLICY_VERSION = 'rules-v3-aircraft'
POLICIES = {
    'priority_distance': '优先级＋最近车辆＋最短距离',
    'deadline_forecast': '截止时间优先＋预计完成时间选车＋预测等待路径',
    'rolling_optimization': '滚动联合任务分配＋排队预测＋预测等待路径',
}


def _forecast_departure(world, node_id, arrival, speed):
    """Approximate pass-through time using public forecasts/current granted state."""
    if world.layout.nodes[node_id].type != 'crossing':
        return arrival
    windows = []
    for request in world.occupancies.values():
        if request.crossing_id != node_id or request.status == 'completed':
            continue
        if request.status == 'active':
            windows.append((world.now, request.actual_end))
        else:
            # Even waiting_clearance uses the public forecast; no oracle of
            # future grant time or future request drift is exposed to planning.
            windows.append((request.predicted_start, request.predicted_end))
    from .crossings import zone_radius
    clearance = timedelta(seconds=2 * zone_radius(world.layout, node_id) / speed)
    for begin, end in sorted(windows):
        if arrival < end and arrival + clearance > begin:
            arrival = end
    return arrival


def forecast_path(world, vehicle, start, target, departure=None):
    layout = world.layout
    if start not in layout.nodes or target not in layout.nodes:
        raise ValueError('unknown route endpoint')
    departure = departure or world.now
    arrivals = {start: departure}
    previous = {}
    heap = [(departure, start)]
    while heap:
        arrival, node = heapq.heappop(heap)
        if arrival != arrivals[node]:
            continue
        if node == target:
            break
        for nxt, distance, _ in layout.adjacency.get(node, []):
            if layout.is_blocked(node, nxt):
                continue
            speed = min(vehicle.speed_mps, layout.speed_limit(node, nxt))
            if speed <= 0:
                continue
            candidate = _forecast_departure(world, nxt, arrival + timedelta(seconds=distance / speed), speed)
            if nxt not in arrivals or candidate < arrivals[nxt]:
                arrivals[nxt] = candidate
                previous[nxt] = node
                heapq.heappush(heap, (candidate, nxt))
    if target not in arrivals:
        raise ValueError(f'no path from {start} to {target}')
    path = [target]
    while path[-1] != start:
        path.append(previous[path[-1]])
    return list(reversed(path)), arrivals[target]


def _measure(world, started):
    stats = world.policy_timing
    elapsed = perf_counter() - started
    stats['calls'] += 1
    stats['total_s'] += elapsed
    stats['max_s'] = max(stats['max_s'], elapsed)


def route(world, vehicle, start, target):
    started = perf_counter()
    try:
        if world.policy_name in ('deadline_forecast', 'rolling_optimization'):
            return forecast_path(world, vehicle, start, target)[0]
        return world.layout.shortest_path(start, target)
    finally:
        _measure(world, started)


def task_order(world):
    from .aircraft import task_ready
    return scheduler.pick_task_order([t for t in world.tasks.values() if task_ready(world,t)],
            strategy='deadline' if world.policy_name == 'deadline_forecast' else 'priority', now=world.now)


def select_vehicle(world, task):
    started = perf_counter()
    try:
        if world.policy_name == 'priority_distance':
            return scheduler.assign_nearest(task, list(world.vehicles.values()), world.layout)
        candidates = []
        for vehicle in scheduler._compatible_idle_vehicles(task, list(world.vehicles.values())):
            try:
                finish = estimate_finish(world, vehicle, task)
                candidates.append((finish, vehicle.id, vehicle))
            except ValueError:
                continue
        return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None
    finally:
        _measure(world, started)


UNKNOWN_WAIT_PENALTY_S = 120.
ROLLING_INTERVAL_S = 15.
MAX_TASKS = 6
MAX_VEHICLES = 5
MAX_SEARCH_NODES = 20000


def estimate_finish(world, vehicle, task):
    """Public-window ETA plus finite-server depot queues and service duration."""
    from . import facilities
    from .simulator import SERVICE_DURATION_S
    _, pickup = forecast_path(world, vehicle, vehicle.node_id, task.origin)
    waiting = facilities.forecast_wait(world, task.origin, pickup, vehicle.id)
    pickup += timedelta(seconds=UNKNOWN_WAIT_PENALTY_S if waiting is None else waiting)
    pickup += timedelta(seconds=world.facility_config.get(task.origin, {}).get('pickup_s',0))
    _, finish = forecast_path(world, vehicle, task.origin, task.destination, pickup)
    waiting = facilities.forecast_wait(world, task.destination, finish, vehicle.id)
    finish += timedelta(seconds=UNKNOWN_WAIT_PENALTY_S if waiting is None else waiting)
    finish += timedelta(seconds=task.service_duration_s or SERVICE_DURATION_S[task.type.value])
    return finish


def rolling_assignments(world):
    """Bounded joint task/vehicle matching, repeated as the observed world changes.

    First maximize assignments, then minimize priority-weighted tardiness plus
    0.01 * completion time. Only idle vehicles and released dependency-ready
    tasks are committed; running jobs are never rewritten. No external solver.
    """
    from .models import VehicleState
    from .simulator import record_event
    if world.next_dispatch_at and world.now < world.next_dispatch_at:
        return []
    world.next_dispatch_at = world.now + timedelta(seconds=ROLLING_INTERVAL_S)
    from .aircraft import task_ready
    pending = scheduler.pick_task_order([t for t in world.tasks.values() if task_ready(world,t)], strategy='deadline', now=world.now)
    # Aging prevents an endless stream of new urgent jobs hiding old work.
    pending.sort(key=lambda t: (-int((world.now-t.created_at).total_seconds()//300),
                                t.deadline or t.created_at, -t.priority, t.id))
    tasks = pending[:MAX_TASKS]
    vehicles = sorted([v for v in world.vehicles.values() if v.state == VehicleState.IDLE
                       and v.current_task_id is None and v.next_node_id is None and v.target_node_id is None], key=lambda v:v.id)
    if not tasks or not vehicles:
        return []
    started = perf_counter()
    try:
        costs = {}
        for i,t in enumerate(tasks):
            for j,v in enumerate(vehicles):
                if v not in scheduler._compatible_idle_vehicles(t,vehicles):
                    continue
                try:
                    finish = estimate_finish(world,v,t)
                except ValueError:
                    continue
                late = max(0., (finish-t.deadline).total_seconds()) if t.deadline else 0.
                costs[i,j] = t.priority*late + .01*(finish-world.now).total_seconds()
        if len(vehicles)>MAX_VEHICLES:
            # Cover each pending task type before filling the bounded search.
            # Truncating alphabetical IDs can permanently omit every tractor.
            selected=[]
            for kind in dict.fromkeys(t.type for t in tasks):
                candidates=[(cost,vehicles[j].id,j) for (i,j),cost in costs.items()
                            if tasks[i].type==kind and j not in selected]
                if candidates:
                    selected.append(min(candidates)[2])
            remaining=sorted((min(cost for (i,k),cost in costs.items() if k==j),vehicles[j].id,j)
                             for j in range(len(vehicles)) if j not in selected and any(k==j for _,k in costs))
            selected.extend(j for _,_,j in remaining[:MAX_VEHICLES-len(selected)])
            costs={(i,new):costs[i,old] for new,old in enumerate(selected)
                   for i in range(len(tasks)) if (i,old) in costs}
            vehicles=[vehicles[j] for j in selected]
        deferred = {i:min((c for (k,_),c in costs.items() if k==i),default=0.)
                    + (t.priority+.01)*ROLLING_INTERVAL_S for i,t in enumerate(tasks)}
        best_key, best_pairs = (1, float('inf'), ()), []
        visited = 0
        def search(j, used, pairs, cost):
            nonlocal best_key, best_pairs, visited
            if visited >= MAX_SEARCH_NODES:
                return
            visited += 1
            key = (-len(pairs), cost+sum(c for i,c in deferred.items() if i not in used),
                   tuple((tasks[i].id,vehicles[k].id) for i,k in pairs))
            if key < best_key:
                best_key, best_pairs = key, list(pairs)
            if j == len(vehicles):
                return
            for i in range(len(tasks)):
                if i in used or (i,j) not in costs:
                    continue
                # Queue pressure from simultaneous planned pickups at the same
                # depot is included in the joint objective, not just pair ETAs.
                prior = sum(tasks[k].origin == tasks[i].origin for k,_ in pairs)
                config = world.facility_config.get(tasks[i].origin)
                contention = prior*config['pickup_s']/config['capacity'] if config else 0.
                search(j+1, used|{i}, pairs+[(i,j)], cost+costs[i,j]+contention)
            search(j+1,used,pairs,cost)
        search(0,set(),[],0.)
        stats = world.dispatch_stats
        stats['epochs'] += 1
        stats['search_nodes'] += visited
        stats['truncated_epochs'] += visited >= MAX_SEARCH_NODES
        record_event(world,'rolling_plan','scheduler', assignments=[{'task_id':tasks[i].id,'vehicle_id':vehicles[j].id} for i,j in best_pairs],
                     objective=best_key[1], candidates=len(tasks), search_nodes=visited,
                     truncated=visited>=MAX_SEARCH_NODES, interval_s=ROLLING_INTERVAL_S)
        return [(tasks[i],vehicles[j]) for i,j in best_pairs]
    finally:
        _measure(world,started)


def assignments(world):
    if world.policy_name == 'rolling_optimization':
        yield from rolling_assignments(world)
    else:
        for task in task_order(world):
            vehicle = select_vehicle(world,task)
            if vehicle:
                yield task,vehicle
