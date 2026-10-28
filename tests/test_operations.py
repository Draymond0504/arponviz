from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apronops import checkpoint, simulator
from apronops.api import main
from apronops.layout import Layout, Node
from apronops.models import Task, TaskStatus, TaskType, Vehicle, VehicleState, VehicleType
from apronops.storage import RuntimeSnapshotORM, get_session, init_db


def world_for_ops():
    layout = Layout()
    layout.nodes = {k: Node(k, k, 'road', x, y) for k, x, y in
                    [('A', 0., 0.), ('B', 20., 0.), ('C', 40., 0.), ('D', 20., 30.)]}
    for a, b, length in [('A', 'B', 20.), ('B', 'C', 20.), ('A', 'D', 36.), ('D', 'C', 36.)]:
        layout.adjacency.setdefault(a, []).append((b, length, length / 4))
        layout.adjacency.setdefault(b, []).append((a, length, length / 4))
    now = datetime.utcnow()
    vehicles = {k: Vehicle(id=k, type=VehicleType.TRACTOR, node_id=node, x=layout.nodes[node].x,
                          y=layout.nodes[node].y, speed_mps=4., last_heartbeat=now)
                for k, node in [('V1', 'A'), ('V2', 'D')]}
    task = Task(id='T1', type=TaskType.BAGGAGE_TOW, origin='B', destination='C', created_at=now)
    return simulator.World(layout, vehicles, {task.id: task})


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(simulator, '_persist', lambda w: None)
    return world_for_ops()


def unresolved_alert(world, kind):
    return next(a for a in world.alerts.values() if a.anomaly_type == kind and a.resolved_at is None)


def test_pre_pickup_failure_reassigns_and_repaired_vehicle_returns_without_teleport(world):
    simulator.tick(world, 1.)
    old = world.vehicles['V1']
    before = (old.x, old.y)
    assert old.edge_progress_m > 0
    simulator.inject_anomaly(world, 'communication_lost', old.id)
    assert world.tasks['T1'].status == TaskStatus.REASSIGNED
    assert old.current_task_id is None and old.state == VehicleState.DEGRADED
    simulator.tick(world, 1.)
    assert world.tasks['T1'].assigned_vehicle_id == 'V2'
    simulator.resolve_alert(world, unresolved_alert(world, 'communication_lost').id, '修复通信')
    assert (old.x, old.y) == before
    assert old.state == VehicleState.RETURNING
    simulator.tick(world, 0.1)
    assert old.x >= before[0]  # Finish the open segment before returning to A.
    assert old.current_task_id is None
    assert sum(e.event_type == 'task_requeued' for e in world.pending_events) == 1


def test_loaded_task_resumes_only_after_all_faults_clear(world):
    vehicle, task = world.vehicles['V1'], world.tasks['T1']
    vehicle.node_id, vehicle.x = 'B', 20.
    simulator._start_assignment(world, vehicle, task, world.now)
    simulator._advance_vehicle(world, vehicle, 1., world.now)
    simulator._advance_vehicle(world, vehicle, 1., world.now)
    assert vehicle.task_phase == 'to_destination' and vehicle.edge_progress_m > 0
    position = (vehicle.x, vehicle.y, vehicle.edge_progress_m)
    simulator.inject_anomaly(world, 'communication_lost', vehicle.id)
    simulator.inject_anomaly(world, 'position_quality_degraded', vehicle.id)
    assert task.assigned_vehicle_id == vehicle.id and task.status == TaskStatus.IN_PROGRESS
    simulator.resolve_alert(world, unresolved_alert(world, 'communication_lost').id, '通信恢复')
    assert vehicle.state == VehicleState.DEGRADED
    simulator.tick(world, 5.)
    assert (vehicle.x, vehicle.y, vehicle.edge_progress_m) == position
    alert = unresolved_alert(world, 'position_quality_degraded')
    simulator.resolve_alert(world, alert.id, '定位恢复')
    assert vehicle.state == VehicleState.OPERATING
    simulator.resolve_alert(world, alert.id, '重复操作')
    assert alert.resolution == '定位恢复'
    simulator.tick(world, 1.)
    assert vehicle.x > position[0]


def test_service_progress_survives_fault(world):
    vehicle, task = world.vehicles['V1'], world.tasks['T1']
    vehicle.node_id, vehicle.x = 'C', 40.
    vehicle.state, vehicle.task_phase = VehicleState.OPERATING, 'servicing'
    vehicle.current_task_id, vehicle.service_remaining_s = task.id, 12.
    task.status, task.assigned_vehicle_id = TaskStatus.IN_PROGRESS, vehicle.id
    simulator.inject_anomaly(world, 'position_quality_degraded', vehicle.id)
    simulator.tick(world, 5.)
    assert vehicle.service_remaining_s == 12.
    simulator.resolve_alert(world, unresolved_alert(world, 'position_quality_degraded').id, '恢复')
    simulator.tick(world, 5.)
    assert vehicle.service_remaining_s == 7.
    simulator.tick(world, 7.)
    assert task.status == TaskStatus.COMPLETED


def test_closure_reroutes_at_node_and_resume_after_reopening(world):
    v = world.vehicles['V1']
    v.target_node_id = 'C'
    first = simulator.close_road(world, 'A', 'B', '施工')
    assert simulator.close_road(world, 'B', 'A', '重复') is first
    assert world.layout.shortest_path('A', 'C') == ['A', 'D', 'C']
    # Block both exits. A no-route outcome must not crash the tick.
    second = simulator.close_road(world, 'A', 'D', '第二处施工')
    assert simulator._move_along_path(world, v, 2.) is False
    assert v.x == 0. and v.id in world.route_wait
    simulator._move_along_path(world, v, 2.)
    assert sum(e.event_type == 'route_waiting' for e in world.pending_events) == 1
    simulator.resolve_alert(world, second['alert_id'], '施工结束')
    simulator._move_along_path(world, v, 1.)
    assert v.next_node_id == 'D' and v.id not in world.route_wait
    assert world.layout.is_blocked('A', 'B')


def test_mid_edge_closure_stops_without_resetting_progress(world):
    v = world.vehicles['V1']
    v.target_node_id = 'C'
    simulator._move_along_path(world, v, 1.)
    location = (v.x, v.y, v.edge_progress_m)
    closure = simulator.close_road(world, 'A', 'B', '阻塞')
    simulator._move_along_path(world, v, 5.)
    assert (v.x, v.y, v.edge_progress_m) == location
    simulator.reopen_road(world, closure['id'], '清理完成')
    simulator._move_along_path(world, v, 1.)
    assert v.x > location[0]


def test_disconnected_task_stays_pending(world):
    simulator.close_road(world, 'B', 'C', '封闭')
    simulator.close_road(world, 'D', 'C', '封闭')
    simulator.tick(world, 5.)
    assert world.tasks['T1'].status == TaskStatus.PENDING
    assert all(v.state == VehicleState.IDLE for v in world.vehicles.values())


def test_snapshot_restores_full_motion_fault_and_closure_state(tmp_path):
    init_db(str(tmp_path / 'snapshot.db'))
    world = world_for_ops()
    simulator.tick(world, 1.)
    simulator.inject_anomaly(world, 'position_quality_degraded', 'V1')
    simulator.close_road(world, 'B', 'C', '施工')
    world.crossing_wait['V2'] = 1.25
    simulator._persist(world)
    with get_session() as session:
        payload = session.get(RuntimeSnapshotORM, 1).payload
    restored = checkpoint.restore_world(world_for_ops().layout, payload)
    assert checkpoint.dump_world(restored) == checkpoint.dump_world(world)
    assert restored.layout.is_blocked('B', 'C')
    for item in (world, restored):
        simulator.resolve_alert(item, unresolved_alert(item, 'position_quality_degraded').id, '恢复')
        simulator.tick(item, 1.)
    assert [v.model_dump() for v in restored.vehicles.values()] == [v.model_dump() for v in world.vehicles.values()]
    assert restored.now == world.now
    changed_layout = world_for_ops().layout
    changed_layout.nodes['A'].x = 99.
    with pytest.raises(ValueError, match='layout differs'):
        checkpoint.restore_world(changed_layout, payload)


def test_failed_persistence_retains_pending_events(tmp_path, monkeypatch):
    init_db(str(tmp_path / 'failed.db'))
    world = world_for_ops()
    simulator.record_event(world, 'test_event', 'operator')
    session = get_session()
    def fail():
        raise RuntimeError('disk full')
    monkeypatch.setattr(session, 'commit', fail)
    monkeypatch.setattr(simulator, 'get_session', lambda: session)
    with pytest.raises(RuntimeError, match='disk full'):
        simulator._persist(world)
    assert len(world.pending_events) == 1


def test_api_mutations_are_durable_and_restart_is_repeatable(tmp_path, monkeypatch):
    monkeypatch.setenv('APRONOPS_DB_PATH', str(tmp_path / 'api.db'))
    monkeypatch.setattr(main, 'TICK_INTERVAL_S', 3600.)
    with TestClient(main.app) as client:
        response = client.post('/tasks', json=dict(id='CUSTOM', type='inspection', origin='DEPOT_AMR', destination='CHECK_N'))
        assert response.status_code == 200
        assert client.post('/tasks', json=dict(id='BAD', type='inspection', origin='DEPOT_AMR', destination='CHECK_N', priority=6)).status_code == 422
        e = client.get('/layout').json()['edges'][0]
        response = client.post('/roads/closures', json=dict(from_node=e['from'], to_node=e['to'], reason='施工'))
        assert response.status_code == 200
        closure = response.json()
        state = client.get('/state').json()
        assert client.get('/health').status_code == 200
    for _ in range(2):
        with TestClient(main.app) as client:
            assert client.get('/state').json() == state
            assert client.get('/tasks').json()[-1]['id'] == 'CUSTOM'
    with TestClient(main.app) as client:
        assert client.post(f"/alerts/{closure['alert_id']}/resolve", json={'resolution': '道路恢复'}).status_code == 200
        assert client.get('/roads/closures').json()[0]['reopened_at'] is not None
        assert not any(e['closed'] for e in client.get('/layout').json()['edges'])


def test_api_rollback_on_commit_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('APRONOPS_DB_PATH', str(tmp_path / 'rollback.db'))
    monkeypatch.setattr(main, 'TICK_INTERVAL_S', 3600.)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        before = client.get('/state').json()
        def fail(world):
            raise RuntimeError('disk full')
        monkeypatch.setattr(simulator, '_persist', fail)
        result = client.post('/tasks', json=dict(id='CUSTOM', type='inspection', origin='DEPOT_AMR', destination='CHECK_N'))
        assert result.status_code == 500
        assert client.get('/state').json() == before


def test_metrics_include_unfinished_due_tasks(tmp_path):
    from apronops.metrics import compute_metrics
    init_db(str(tmp_path / 'metrics.db'))
    world = world_for_ops()
    a = world.tasks['T1']
    a.deadline = world.now - timedelta(seconds=10)
    b = a.model_copy(update=dict(id='T2', status=TaskStatus.COMPLETED, completed_at=a.deadline - timedelta(seconds=1)))
    world.tasks[b.id] = b
    simulator._persist(world)
    result = compute_metrics(world.now)
    assert result['on_time_rate'] == .5
    assert result['completed_on_time_rate'] == 1.
    assert result['overdue_unfinished'] == 1


def test_legacy_database_is_not_silently_overwritten(tmp_path):
    from apronops.storage import VehicleORM
    init_db(str(tmp_path / 'legacy.db'))
    world = world_for_ops()
    v = world.vehicles['V1']
    with get_session() as session:
        session.add(VehicleORM(**v.model_dump(exclude={'type', 'state', 'planned_path'}), type=v.type.value, state=v.state.value))
        session.commit()
    with pytest.raises(ValueError, match='Legacy database'):
        checkpoint.load_or_create_world()
    with get_session() as session:
        assert session.get(VehicleORM, 'V1') is not None
        assert session.get(RuntimeSnapshotORM, 1) is None


def test_reassigned_task_finishes_exactly_once(world):
    simulator.tick(world, 1.)
    simulator.inject_anomaly(world, 'communication_lost', 'V1')
    simulator.tick(world, 1.)
    simulator.resolve_alert(world, unresolved_alert(world, 'communication_lost').id, '修复')
    for _ in range(200):
        simulator.tick(world, 5.)
    assert world.tasks['T1'].status == TaskStatus.COMPLETED
    assert all(v.state == VehicleState.IDLE for v in world.vehicles.values())
    assert sum(e.event_type == 'task_completed' for e in world.pending_events) == 1


def test_background_failure_rolls_back_tick_and_reports_paused(monkeypatch):
    from fastapi import HTTPException
    class RunOnce:
        def wait(self, timeout):
            return False
    world = world_for_ops()
    monkeypatch.setattr(main, '_world', world)
    monkeypatch.setattr(main, '_runtime_error', None)
    monkeypatch.setattr(main, '_stop_event', RunOnce())
    before = world.now
    def fail(world, **kwargs):
        world.now += timedelta(seconds=5)
        world.vehicles['V1'].x = 999.
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(simulator, 'tick', fail)
    main._background_loop()
    assert main._world.now == before
    assert main._world.vehicles['V1'].x == 0.
    with pytest.raises(HTTPException) as error:
        main.health()
    assert error.value.status_code == 503
    assert main.get_state()['runtime_error']
