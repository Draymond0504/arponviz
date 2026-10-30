"""Interchangeable rule policies; forecasts never read future requested windows."""
from __future__ import annotations

import heapq
from datetime import timedelta
from time import perf_counter

from . import scheduler

POLICY_VERSION = 'rules-v1'
POLICIES = {
    'priority_distance': '优先级＋最近车辆＋最短距离',
    'deadline_forecast': '截止时间优先＋预计完成时间选车＋预测等待路径',
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
        if world.policy_name == 'deadline_forecast':
            return forecast_path(world, vehicle, start, target)[0]
        return world.layout.shortest_path(start, target)
    finally:
        _measure(world, started)


def task_order(world):
    return scheduler.pick_task_order(list(world.tasks.values()),
            strategy='deadline' if world.policy_name == 'deadline_forecast' else 'priority', now=world.now)


def select_vehicle(world, task):
    started = perf_counter()
    try:
        if world.policy_name == 'priority_distance':
            return scheduler.assign_nearest(task, list(world.vehicles.values()), world.layout)
        candidates = []
        for vehicle in scheduler._compatible_idle_vehicles(task, list(world.vehicles.values())):
            try:
                _, pickup = forecast_path(world, vehicle, vehicle.node_id, task.origin)
                _, finish = forecast_path(world, vehicle, task.origin, task.destination, pickup)
                candidates.append((finish, vehicle.id, vehicle))
            except ValueError:
                continue
        return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None
    finally:
        _measure(world, started)
