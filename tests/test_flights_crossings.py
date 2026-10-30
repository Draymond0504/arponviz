from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apronops import checkpoint, crossings, flights, scheduler, simulator
from apronops.api import main
from apronops.layout import Layout, Node
from apronops.models import CrossingOccupancy, Flight, TaskStatus, Vehicle, VehicleState, VehicleType


def flight(world, **changes):
    values = dict(id='TEST101', stand_id='SP_20', arrival_at=world.now + timedelta(seconds=20),
                  departure_at=world.now + timedelta(seconds=2400))
    values.update(changes)
    return Flight(**values)


def line_world():
    layout = Layout()
    layout.nodes = {key: Node(key, key, 'crossing' if key == 'X' else 'road', x, 0.)
                    for key, x in [('A', 0.), ('X', 20.), ('B', 40.)]}
    layout.adjacency = {'A': [('X', 20., 5.)], 'X': [('A', 20., 5.), ('B', 20., 5.)], 'B': [('X', 20., 5.)]}
    v = Vehicle(id='V', type=VehicleType.TRACTOR, node_id='A', target_node_id='B',
                x=0., y=0., speed_mps=4., last_heartbeat=datetime.utcnow(),
                state=VehicleState.RETURNING, task_phase='returning')
    return simulator.World(layout, {'V': v}, {})


def request(world, start=0., duration=20., **changes):
    values = dict(id='O1', crossing_id='X', aircraft_id='AIR1',
                  predicted_start=world.now, predicted_end=world.now + timedelta(seconds=5),
                  requested_start=world.now + timedelta(seconds=start),
                  requested_end=world.now + timedelta(seconds=start+duration))
    values.update(changes)
    return CrossingOccupancy(**values)


@pytest.fixture(autouse=True)
def no_storage(monkeypatch):
    original = simulator._persist
    monkeypatch.setattr(simulator, '_persist', lambda w: None)
    return original


def test_flight_creation_is_idempotent_and_gates_dependencies():
    world = simulator.load_world()
    original = flight(world)
    flights.upsert_flight(world, original)
    flights.upsert_flight(world, original)
    linked = [t for t in world.tasks.values() if t.flight_id == original.id]
    assert len(linked) == 3 and world.flights[original.id].revision == 1
    assert not scheduler.pick_task_order(linked, now=world.now)
    ready = scheduler.pick_task_order(linked, now=original.arrival_at)
    assert {t.type.value for t in ready} == {'baggage_tow', 'catering_supply'}
    check = next(t for t in linked if t.type.value == 'inspection')
    for t in ready:
        t.status = TaskStatus.COMPLETED
    assert scheduler.pick_task_order(linked, now=original.arrival_at) == [check]


def test_delayed_flight_updates_pending_windows_but_keeps_started_progress():
    world = simulator.load_world()
    f = flights.upsert_flight(world, flight(world))
    tasks = [t for t in world.tasks.values() if t.flight_id == f.id]
    tasks[0].status = TaskStatus.IN_PROGRESS
    tasks[0].started_at = world.now
    old_release = tasks[0].release_at
    changed = f.model_copy(update={'arrival_at': f.arrival_at+timedelta(minutes=5), 'departure_at': f.departure_at+timedelta(minutes=5)})
    flights.upsert_flight(world, changed, expected_revision=1)
    started = world.tasks[tasks[0].id]
    assert started.started_at == world.now and started.release_at == old_release
    assert started.deadline == changed.departure_at
    assert world.tasks[tasks[1].id].release_at == changed.arrival_at
    assert len([t for t in world.tasks.values() if t.flight_id == f.id]) == 3
    with pytest.raises(ValueError, match='版本'):
        flights.upsert_flight(world, f, expected_revision=1)
    with pytest.raises(ValueError, match='更换机位'):
        flights.upsert_flight(world, changed.model_copy(update={'stand_id': 'SP_01'}), expected_revision=2)


def test_flight_tasks_finish_with_inspection_after_both_services():
    world = simulator.load_world()
    world.tasks.clear()
    f = flights.upsert_flight(world, flight(world, arrival_at=world.now+timedelta(seconds=2)))
    for _ in range(700):
        simulator.tick(world, 5.)
        if all(t.status == TaskStatus.COMPLETED for t in world.tasks.values()):
            break
    assert all(t.status == TaskStatus.COMPLETED for t in world.tasks.values())
    check = next(t for t in world.tasks.values() if t.type.value == 'inspection')
    assert check.started_at >= max(world.tasks[tid].completed_at for tid in check.depends_on)
    assert all(t.started_at >= f.arrival_at for t in world.tasks.values())
    assert flights.flight_rows(world)[0]['status'] == '保障完成'


def test_aircraft_occupancy_stops_vehicle_before_zone_and_then_releases():
    world = line_world()
    crossings.register_occupancy(world, request(world))
    simulator.tick(world, 15.)
    v = world.vehicles['V']
    assert v.x == pytest.approx(17.) and v.current_speed_mps == 0.
    assert not crossings.vehicle_in_zone(world, v, 'X')
    assert '等待飞机通行' in world.route_wait[v.id]
    assert world.occupancies['O1'].status == 'active'
    simulator.tick(world, 20.)
    assert v.x > 23.
    assert world.occupancies['O1'].status == 'completed'
    assert v.id not in world.route_wait


def test_future_window_is_not_skipped_by_large_tick_and_prediction_is_not_authority():
    world = line_world()
    # Predicted 0..5, requested 4.25..14.25: must obey requested occupancy.
    r = crossings.register_occupancy(world, request(world, start=4.25, duration=10.))
    simulator.tick(world, 12.)
    assert world.vehicles['V'].x <= 17.
    assert r.actual_start == r.requested_start
    assert r.actual_end == r.requested_end
    assert r.status == 'active'
    simulator.tick(world, 12.)
    assert world.vehicles['V'].x > 23.


def test_aircraft_waits_for_vehicle_egress_and_gets_full_duration():
    world = line_world()
    v = world.vehicles['V']
    v.next_node_id, v.edge_progress_m, v.x = 'X', 18., 18.
    v.planned_path = ['A', 'X', 'B']
    r = crossings.register_occupancy(world, request(world, duration=4.))
    crossings.update_crossings(world)
    assert r.status == 'waiting_clearance'
    for _ in range(60):
        simulator.tick(world, .5)
        if r.status == 'active':
            break
    assert r.status == 'active'
    assert v.x >= 23.
    assert r.actual_start > r.requested_start
    assert (r.actual_end - r.actual_start).total_seconds() == 4.


def test_fault_inside_zone_cannot_release_aircraft_early():
    world = line_world()
    v = world.vehicles['V']
    v.node_id, v.x, v.state = 'X', 20., VehicleState.DEGRADED
    r = crossings.register_occupancy(world, request(world, duration=3.))
    simulator.tick(world, 20.)
    assert r.status == 'waiting_clearance' and r.actual_start is None
    assert v.x == 20.


def test_two_vehicles_and_overlapping_aircraft_requests_are_exclusive():
    world = line_world()
    second = world.vehicles['V'].model_copy(update={'id': 'V2', 'node_id': 'B', 'x': 40., 'target_node_id': 'A'})
    world.vehicles[second.id] = second
    crossings.register_occupancy(world, request(world, start=10., duration=5.))
    crossings.register_occupancy(world, request(world, start=11., duration=3., id='O2', aircraft_id='AIR2'))
    for _ in range(100):
        simulator.tick(world, .5)
        inside = [v for v in world.vehicles.values() if crossings.vehicle_in_zone(world, v, 'X')]
        active = [r for r in world.occupancies.values() if r.status == 'active']
        assert len(inside) <= 1
        assert len(active) <= 1
        assert not (inside and active)


def test_v1_snapshot_migrates_without_losing_existing_tasks():
    world = simulator.load_world()
    p = checkpoint.dump_world(world)
    p['version'] = 1
    for k in ('flights', 'occupancies', 'crossing_tokens'):
        p.pop(k)
    for t in p['tasks']:
        for k in ('flight_id', 'release_at', 'depends_on', 'service_duration_s'):
            t.pop(k)
    restored = checkpoint.restore_world(world.layout, p)
    assert set(restored.tasks) == set(world.tasks)
    assert not restored.flights and not restored.occupancies
    assert checkpoint.dump_world(restored)['version'] == 2


def test_new_snapshot_preserves_flights_requests_and_gate_ownership():
    world = simulator.load_world()
    flights.upsert_flight(world, flight(world))
    crossings.register_occupancy(world, request(world, crossing_id='X_10'))
    crossings.update_crossings(world)
    payload = checkpoint.dump_world(world)
    restored = checkpoint.restore_world(world.layout, payload)
    assert checkpoint.dump_world(restored) == payload


def test_api_flights_occupancies_and_restart(tmp_path, monkeypatch, no_storage):
    monkeypatch.setattr(simulator, '_persist', no_storage)
    monkeypatch.setattr(main, 'TICK_INTERVAL_S', 3600.)
    monkeypatch.setenv('APRONOPS_DB_PATH', str(tmp_path / 'flights.db'))
    with TestClient(main.app) as client:
        now = datetime.fromisoformat(client.get('/state').json()['simulation_time'])
        body = dict(id='API_FL1', stand_id='SP_20', arrival_at=(now+timedelta(minutes=1)).isoformat()+'Z',
                    departure_at=(now+timedelta(minutes=50)).isoformat()+'Z')
        assert client.post('/flights', json=body).status_code == 200
        assert client.post('/flights', json=body).status_code == 200
        assert len([t for t in client.get('/tasks').json() if t['flight_id']]) == 3
        bad = {**body, 'arrival_at': now.isoformat()}
        assert client.post('/flights', json=bad).status_code == 422
        bad = {**body, 'departure_at': body['arrival_at']}
        assert client.post('/flights', json=bad).status_code == 422
        bad = {**body, 'stand_id': 'X_01'}
        assert client.post('/flights', json=bad).status_code == 409
        assert client.post('/tasks', json=dict(id='PARK_X', type='inspection', origin='DEPOT_AMR', destination='X_01')).status_code == 400
        updated = {**body, 'arrival_at': (now+timedelta(minutes=10)).isoformat()+'Z', 'expected_revision': 1}
        assert client.post('/flights', json=updated).status_code == 200
        assert client.post('/flights', json={**body, 'expected_revision': 1}).status_code == 409
        occupancy = dict(id='OC1', crossing_id='X_10', aircraft_id='AIR1',
                         predicted_start=(now+timedelta(seconds=5)).isoformat()+'Z',
                         predicted_end=(now+timedelta(seconds=15)).isoformat()+'Z',
                         requested_start=(now+timedelta(seconds=10)).isoformat()+'Z',
                         requested_end=(now+timedelta(seconds=20)).isoformat()+'Z')
        assert client.post('/crossings/occupancies', json=occupancy).status_code == 200
        assert client.post('/crossings/occupancies', json=occupancy).status_code == 200
        assert len(client.get('/crossings/occupancies').json()) == 1
        assert client.post('/crossings/occupancies', json={**occupancy, 'crossing_id': 'SP_20'}).status_code == 409
        state = client.get('/state').json()
    with TestClient(main.app) as client:
        assert client.get('/state').json() == state
        assert client.get('/flights').json()[0]['revision'] == 2
        assert client.post('/scenarios/turnaround-demo').status_code == 200
        assert client.post('/scenarios/turnaround-demo').status_code == 200
        assert len(client.get('/flights').json()) == 2
        assert len(client.get('/crossings/occupancies').json()) == 3


def test_fault_and_restart_inside_zone_preserve_exclusion():
    world = line_world()
    v = world.vehicles['V']
    v.node_id, v.x = 'X', 20.
    world.crossing_tokens['X'] = v.id
    v.state = VehicleState.DEGRADED
    crossings.register_occupancy(world, request(world, duration=5.))
    simulator.tick(world, 1.)
    restored = checkpoint.restore_world(line_world().layout, checkpoint.dump_world(world))
    simulator.tick(restored, 10.)
    assert restored.occupancies['O1'].status == 'waiting_clearance'
    assert restored.crossing_tokens['X'] == 'V'
    restored.vehicles['V'].state = VehicleState.RETURNING
    for _ in range(30):
        simulator.tick(restored, .5)
    assert restored.occupancies['O1'].status == 'completed'


def test_release_inside_large_tick_and_granularity_agree():
    world = simulator.load_world()
    world.tasks.clear()
    flights.upsert_flight(world, flight(world, arrival_at=world.now+timedelta(seconds=2.25)))
    other = checkpoint.restore_world(world.layout, checkpoint.dump_world(world))
    simulator.tick(world, 5.)
    for _ in range(10):
        simulator.tick(other, .5)
    # Step grids differ at release, but neither version can start before release.
    for w in (world, other):
        assigned = [e for e in w.pending_events if e.event_type == 'task_assigned']
        assert assigned and min(e.timestamp for e in assigned) >= w.flights['TEST101'].arrival_at


def test_prediction_only_does_not_block_vehicle():
    world = line_world()
    crossings.register_occupancy(world, request(world, start=120., duration=10.))
    simulator.tick(world, 15.)
    assert world.vehicles['V'].x > 23.
    assert world.occupancies['O1'].status == 'scheduled'


def test_crossing_owner_can_clear_against_lower_id_opposing_vehicle():
    world = line_world()
    owner = world.vehicles.pop('V')
    owner.id, owner.node_id, owner.x = 'Z', 'X', 20.
    owner.next_node_id, owner.edge_progress_m = 'B', 1.
    owner.x, owner.planned_path = 21., ['X', 'B']
    opposing = owner.model_copy(update={'id': 'A', 'node_id': 'B', 'next_node_id': 'X',
                                       'edge_progress_m': 17., 'x': 23., 'target_node_id': 'A',
                                       'planned_path': ['B', 'X', 'A']})
    world.vehicles = {'Z': owner, 'A': opposing}
    world.crossing_tokens['X'] = owner.id
    crossings.register_occupancy(world, request(world, duration=4.))
    for _ in range(40):
        simulator.tick(world, .5)
        if world.occupancies['O1'].status == 'active':
            break
    assert owner.x >= 23.
    assert world.occupancies['O1'].status == 'active'
