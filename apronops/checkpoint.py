"""Versioned runtime snapshot; API calls this under the world lock.

Snapshots preserve simulation time: server downtime does not advance the world.
Legacy databases are rejected instead of mixing seeded state with old records.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from datetime import datetime

from .models import Aircraft, Alert, CrossingOccupancy, Flight, Task, Vehicle

SNAPSHOT_VERSION = 5

FACILITY_FIELDS = ('facility_config', 'berths', 'berth_reservations', 'depot_queues',
                   'pickup_remaining', 'recovery_detours', 'recovery_route_lock', 'stall_state',
                   'recovery_counts', 'depot_wait_s', 'dispatch_stats')
FACILITY_FIELDS += ('wait_for', 'deadlock_cycles')


def layout_fingerprint(layout) -> str:
    payload = {
        'nodes': [vars(layout.nodes[k]) for k in sorted(layout.nodes)],
        'adjacency': {k: sorted(v) for k, v in sorted(layout.adjacency.items())},
        'attributes': [(a, b, v) for (a, b), v in sorted(layout.edge_attributes.items())],
        'metadata': layout.metadata,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def dump_world(world) -> dict:
    return {
        'facilities': json.loads(json.dumps({k:getattr(world,k) for k in FACILITY_FIELDS})),
        'next_dispatch_at': world.next_dispatch_at.isoformat() if world.next_dispatch_at else None,
        'version': SNAPSHOT_VERSION,
        'layout_fingerprint': layout_fingerprint(world.layout),
        'now': world.now.isoformat(), 'revision': world.revision,
        'seed': world.seed, 'rng_state': json.loads(json.dumps(world.rng.getstate())), 'id_counter': world.id_counter,
        'policy_name': world.policy_name, 'distance_m': world.distance_m, 'empty_distance_m': world.empty_distance_m,
        'vehicles': [v.model_dump(mode='json') for v in world.vehicles.values()],
        'tasks': [t.model_dump(mode='json') for t in world.tasks.values()],
        'alerts': [a.model_dump(mode='json') for a in world.alerts.values()],
        'flights': [f.model_dump(mode='json') for f in world.flights.values()],
        'aircraft': [a.model_dump(mode='json') for a in world.aircraft.values()],
        'aircraft_taxi_owner': world.aircraft_taxi_owner,
        'aircraft_layout_fingerprint': hashlib.sha256(json.dumps(world.layout.reference,sort_keys=True).encode()).hexdigest(),
        'occupancies': [r.model_dump(mode='json') for r in world.occupancies.values()],
        'crossing_tokens': world.crossing_tokens,
        'home_node': world.home_node,
        'crossing_wait': world.crossing_wait,
        'route_wait': world.route_wait,
        'closures': world.closures,
        'suspended_state': {k: v.value for k, v in world.suspended_state.items()},
        'tracks': {k: [[t.isoformat(), x, y] for t, x, y in track] for k, track in world.tracks.items()},
    }


def restore_world(layout, payload):
    from .models import VehicleState
    from .simulator import TRACK_MAXLEN, World

    if payload.get('version') not in (1, 2, 3, 4, SNAPSHOT_VERSION):
        raise ValueError('Unsupported runtime snapshot version; use a separate APRONOPS_DB_PATH.')
    if payload.get('layout_fingerprint') != layout_fingerprint(layout):
        raise ValueError('Road layout differs from saved runtime; use a separate APRONOPS_DB_PATH.')
    if payload.get('aircraft') and payload.get('aircraft_layout_fingerprint') != hashlib.sha256(json.dumps(layout.reference,sort_keys=True).encode()).hexdigest():
        raise ValueError('Aircraft layout differs from saved runtime; use a separate APRONOPS_DB_PATH.')
    config = payload.get('facilities', {}).get('facility_config')
    if config is None:
        # Legacy snapshots had no capacity. Preserve every parked vehicle and
        # record the inferred capacity rather than discarding an old resident.
        from .facilities import DEFAULT
        config = {nid:{**DEFAULT,'capacity':max(DEFAULT['capacity'],sum(
            (v['node_id']==nid and v.get('edge_progress_m',0)<=1e-9)
            or (v.get('next_node_id')==nid and v.get('edge_progress_m',0)>1e-9)
            for v in payload['vehicles']))}
            for nid,n in layout.nodes.items() if n.type == 'depot'}
    world = World(layout, {v['id']: Vehicle.model_validate(v) for v in payload['vehicles']},
                  {t['id']: Task.model_validate(t) for t in payload['tasks']}, depot_config=config)
    world.now = datetime.fromisoformat(payload['now'])
    world.revision = payload['revision']
    world.seed = payload.get('seed')
    if 'rng_state' in payload:
        def tuplify(value):
            return tuple(tuplify(v) for v in value) if isinstance(value, (list, tuple)) else value
        world.rng.setstate(tuplify(payload['rng_state']))
    world.id_counter = payload.get('id_counter', 0)
    world.policy_name = payload.get('policy_name', 'priority_distance')
    if 'facilities' in payload:
        for key in FACILITY_FIELDS:
            setattr(world,key,json.loads(json.dumps(payload['facilities'][key])))
    else:
        from .facilities import free_slot
        # Legacy inbound cars already crossed the new admission point. Reserve
        # their destination without rewinding their physical edge progress.
        for v in world.vehicles.values():
            if v.next_node_id in world.berths and v.edge_progress_m > 1e-9:
                world.berth_reservations[v.next_node_id][v.id] = free_slot(world,v.next_node_id)
    if payload.get('next_dispatch_at'):
        world.next_dispatch_at = datetime.fromisoformat(payload['next_dispatch_at'])
    world.distance_m = dict(payload.get('distance_m', world.distance_m))
    world.empty_distance_m = dict(payload.get('empty_distance_m', world.empty_distance_m))
    world.alerts = {a['id']: Alert.model_validate(a) for a in payload['alerts']}
    world.home_node = dict(payload['home_node'])
    world.crossing_wait = dict(payload['crossing_wait'])
    world.route_wait = dict(payload['route_wait'])
    world.closures = dict(payload['closures'])
    world.flights = {f['id']: Flight.model_validate(f) for f in payload.get('flights', [])}
    world.aircraft = {a['id']: Aircraft.model_validate(a) for a in payload.get('aircraft', [])}
    world.aircraft_taxi_owner = payload.get('aircraft_taxi_owner')
    world.occupancies = {r['id']: CrossingOccupancy.model_validate(r) for r in payload.get('occupancies', [])}
    world.crossing_tokens = dict(payload.get('crossing_tokens', {}))
    if payload['version'] == 1:
        world.crossing_wait.clear()  # Retire the old fixed three-second crossing stop.
    world.suspended_state = {k: VehicleState(v) for k, v in payload['suspended_state'].items()}
    world.tracks = {k: deque([(datetime.fromisoformat(t), x, y) for t, x, y in points], maxlen=TRACK_MAXLEN)
                    for k, points in payload['tracks'].items()}
    layout.blocked_edges = {tuple(sorted((c['from_node'], c['to_node'])))
                            for c in world.closures.values() if c['reopened_at'] is None}
    return world


def load_or_create_world():
    from .storage import AlertORM, EventORM, RuntimeSnapshotORM, TaskORM, VehicleORM, get_session
    from .simulator import load_world, _persist

    seed = load_world()
    with get_session() as session:
        saved = session.get(RuntimeSnapshotORM, 1)
        if saved is not None:
            return restore_world(seed.layout, saved.payload)
        if any(session.query(model).first() is not None for model in (VehicleORM, TaskORM, AlertORM, EventORM)):
            raise ValueError('Legacy database has no complete runtime snapshot. Keep it for history and set a new APRONOPS_DB_PATH.')
    _persist(seed)
    return seed
