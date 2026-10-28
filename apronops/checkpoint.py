"""Versioned runtime snapshot; API calls this under the world lock.

Snapshots preserve simulation time: server downtime does not advance the world.
Legacy databases are rejected instead of mixing seeded state with old records.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from datetime import datetime

from .models import Alert, Task, Vehicle
from .storage import AlertORM, EventORM, RuntimeSnapshotORM, TaskORM, VehicleORM, get_session

SNAPSHOT_VERSION = 1


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
        'version': SNAPSHOT_VERSION,
        'layout_fingerprint': layout_fingerprint(world.layout),
        'now': world.now.isoformat(), 'revision': world.revision,
        'vehicles': [v.model_dump(mode='json') for v in world.vehicles.values()],
        'tasks': [t.model_dump(mode='json') for t in world.tasks.values()],
        'alerts': [a.model_dump(mode='json') for a in world.alerts.values()],
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

    if payload.get('version') != SNAPSHOT_VERSION:
        raise ValueError('Unsupported runtime snapshot version; use a separate APRONOPS_DB_PATH.')
    if payload.get('layout_fingerprint') != layout_fingerprint(layout):
        raise ValueError('Road layout differs from saved runtime; use a separate APRONOPS_DB_PATH.')
    world = World(layout, {v['id']: Vehicle.model_validate(v) for v in payload['vehicles']},
                  {t['id']: Task.model_validate(t) for t in payload['tasks']})
    world.now = datetime.fromisoformat(payload['now'])
    world.revision = payload['revision']
    world.alerts = {a['id']: Alert.model_validate(a) for a in payload['alerts']}
    world.home_node = dict(payload['home_node'])
    world.crossing_wait = dict(payload['crossing_wait'])
    world.route_wait = dict(payload['route_wait'])
    world.closures = dict(payload['closures'])
    world.suspended_state = {k: VehicleState(v) for k, v in payload['suspended_state'].items()}
    world.tracks = {k: deque([(datetime.fromisoformat(t), x, y) for t, x, y in points], maxlen=TRACK_MAXLEN)
                    for k, points in payload['tracks'].items()}
    layout.blocked_edges = {tuple(sorted((c['from_node'], c['to_node'])))
                            for c in world.closures.values() if c['reopened_at'] is None}
    return world


def load_or_create_world():
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
