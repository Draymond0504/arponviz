"""Reproducibility, paired inputs, forecast visibility, and artifact accounting."""
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from apronops import checkpoint, crossings, policies, simulator
from apronops.experiments.engine import Simulation, digest
from apronops.experiments.run import run_one
from apronops.experiments.scenario import Scenario, read_scenario
from apronops.experiments.replay import load_replay, frame_tracks
from apronops.layout import Layout, Node
from apronops.models import CrossingOccupancy, Vehicle, VehicleState, VehicleType

ROOT = Path(__file__).resolve().parents[1]


def short_scenario(**overrides):
    data = dict(id='test', horizon_s=12, sample_s=2,
        tasks=[dict(id='work', type='inspection', origin='DEPOT_AMR', destination='CHECK_N', deadline_s=5),
               dict(id='later', type='inspection', origin='DEPOT_AMR', destination='CHECK_N', release_s=20, deadline_s=30)],
        closures=[dict(id='road', start_s=1.25, end_s=3.75, from_node='DEPOT_AMR', to_node='R_009')],
        faults=[dict(id='signal', start_s=2.25, end_s=4.25, vehicle_id='AMR_INSPECT_01', anomaly_type='communication_lost')],
        crossings=[dict(id='plane', crossing_id='X_11', aircraft_id='SYN', predicted_start_s=2, duration_s=4, shift_jitter_s=1)])
    data.update(overrides)
    return Scenario(**data)


def test_headless_import_and_run_never_load_database_or_api():
    script = """
import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('sqlalchemy','fastapi','streamlit') or fullname == 'apronops.storage':
            raise AssertionError('headless dependency: '+fullname)
sys.meta_path.insert(0, Block())
from apronops.experiments.engine import Simulation
from apronops.experiments.scenario import Scenario
sim = Simulation(Scenario(id='isolated', horizon_s=1))
sim.step(1)
assert sim.elapsed_s == 1
"""
    subprocess.run([sys.executable, '-c', script], cwd=ROOT, check=True, capture_output=True, text=True)


@pytest.mark.parametrize('policy', list(policies.POLICIES))
def test_reproducible_artifacts_reset_and_paired_inputs(tmp_path, policy):
    scenario = short_scenario()
    first, second = tmp_path/'first', tmp_path/'second'
    run_one(scenario, 7, policy, first)
    run_one(scenario, 7, policy, second)
    for name in ('events.jsonl', 'frames.jsonl', 'summary.json', 'inputs.json', 'tasks.csv'):
        assert (first/name).read_bytes() == (second/name).read_bytes(), name
    manifest = json.loads((first/'manifest.json').read_text())
    inputs = json.loads((first/'inputs.json').read_text())
    inputs['initial_world'].pop('policy_name')
    assert digest({'initial': inputs['initial_world'], 'events': inputs['events']}) == manifest['input_sha256']
    a, b = Simulation(scenario, 7, 'priority_distance'), Simulation(scenario, 7, 'deadline_forecast')
    assert a.events == b.events and a.input_hash == b.input_hash
    assert a.input_hash != Simulation(scenario, 8).input_hash
    initial = a.observe()
    a.step(10)
    assert a.reset() == initial
    events = [json.loads(line) for line in (first/'events.jsonl').read_text().splitlines()]
    assert all(e['id'].startswith('sim-') for e in events)
    assert any(e['timestamp'].endswith('01.250000') for e in events)
    assert any(e['timestamp'].endswith('04.250000') for e in events)
    replay = load_replay((first/'replay.json').read_bytes())
    assert replay['frames'] == [json.loads(line) for line in (first/'frames.jsonl').read_text().splitlines()]
    assert replay['frames'][-1]['elapsed_s'] == scenario.horizon_s
    assert frame_tracks(replay['frames'], 1, tail=1)
    with pytest.raises(FileExistsError):
        run_one(scenario, 7, policy, first)


def test_censored_metrics_include_unfinished_and_unreleased_tasks():
    sim = Simulation(short_scenario())
    sim.step(12)
    summary = sim.summary()
    assert summary['released_tasks'] == 1 and summary['unreleased_tasks'] == 1
    assert summary['unfinished_tasks'] == 1 and summary['due_tasks'] == 1
    assert summary['due_on_time_rate'] == 0 and summary['tardiness_lower_bound_s'] == 7
    assert summary['completed_tardiness_p95_s'] is None
    assert summary['conflict_zone_violation_steps'] == 0


@pytest.mark.parametrize('failure,status', [(RuntimeError('injected failure'), 'failed'), (KeyboardInterrupt(), 'interrupted')])
def test_partial_runs_are_saved_and_marked(tmp_path, monkeypatch, failure, status):
    original = Simulation.step
    def fail(self, *args, **kwargs):
        if self.elapsed_s >= .5:
            raise failure
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Simulation, 'step', fail)
    with pytest.raises(type(failure)):
        run_one(short_scenario(), 7, 'priority_distance', tmp_path/'partial')
    replay = load_replay((tmp_path/'partial/replay.json').read_bytes())
    assert replay['manifest']['status'] == replay['summary']['status'] == status
    assert replay['frames'][-1]['elapsed_s'] == .5
    assert replay['summary']['remaining_input_events'] > 0


def test_forecast_uses_public_windows_and_speed_limits_not_future_requests():
    layout = Layout()
    layout.nodes = {nid: Node(nid, nid, 'crossing' if nid == 'X' else 'road', x, y)
                    for nid, x, y in [('A',0,0),('X',10,0),('B',20,0),('D',10,10)]}
    for a,b,d in [('A','X',10),('X','B',10),('A','D',20),('D','B',20)]:
        layout.adjacency.setdefault(a, []).append((b,d,d))
        layout.adjacency.setdefault(b, []).append((a,d,d))
    now = datetime(2026,1,1)
    vehicle = Vehicle(id='V', type=VehicleType.TRACTOR, node_id='A', x=0, y=0, speed_mps=4, last_heartbeat=now)
    w = simulator.World(layout, {'V':vehicle}, {}, now=now, seed=1)
    assert policies.forecast_path(w,vehicle,'A','B')[0] == ['A','X','B']
    r = CrossingOccupancy(id='O',crossing_id='X',aircraft_id='P',predicted_start=now,
                         predicted_end=now+timedelta(seconds=100),requested_start=now+timedelta(seconds=200),
                         requested_end=now+timedelta(seconds=300))
    crossings.register_occupancy(w,r)
    baseline = policies.forecast_path(w,vehicle,'A','B')
    assert baseline[0] == ['A','D','B']
    r.requested_start += timedelta(seconds=500)
    r.requested_end += timedelta(seconds=500)
    assert policies.forecast_path(w,vehicle,'A','B') == baseline
    w.layout.blocked_edges.add(('A','D'))
    assert policies.forecast_path(w,vehicle,'A','B')[0] == ['A','X','B']
    w.layout.blocked_edges.clear()
    w.occupancies.clear()
    layout.edge_attributes['A','X'] = {'speed_limit_mps': .1}
    assert policies.forecast_path(w,vehicle,'A','B')[0] == ['A','D','B']


def test_snapshot_preserves_seed_counter_distance_and_continuation():
    sim = Simulation(short_scenario())
    sim.step(5)
    world = sim.world
    original = json.loads(json.dumps(checkpoint.dump_world(world)))
    restored = checkpoint.restore_world(simulator.load_world().layout, original)
    world.pending_events.clear()
    for w in (world, restored):
        simulator.advance(w, 1)
    assert checkpoint.dump_world(world) == checkpoint.dump_world(restored)
    assert [e.model_dump() for e in world.pending_events] == [e.model_dump() for e in restored.pending_events]
    assert world.new_id() == restored.new_id()
    assert world.rng.random() == restored.rng.random()
    old = dict(original, version=2)
    for key in ('seed','rng_state','id_counter','policy_name','distance_m','empty_distance_m'):
        old.pop(key)
    migrated = checkpoint.restore_world(simulator.load_world().layout, old)
    assert migrated.policy_name == 'priority_distance'
    assert migrated.vehicles.keys() == world.vehicles.keys()


def test_stationary_blocker_explains_unfinished_forecast_task():
    scene = read_scenario(ROOT/'configs/experiments/routing.yaml')
    sim = Simulation(scene, 1, 'deadline_forecast')
    sim.step(1800)
    vehicle = sim.world.vehicles['SHUTTLE_01']
    assert vehicle.next_node_id == 'DEPOT_AMR'
    assert '前方车辆' in sim.world.route_wait[vehicle.id]
    assert sim.summary()['unfinished_tasks'] == 1
    assert sim.summary()['traffic_wait_s'] > 1000
    # Repairing the actual blockage, without changing the policy, allows pickup.
    sim.world.vehicles.pop('AMR_INSPECT_01')
    simulator.advance(sim.world, 15)
    assert sim.world.tasks['INSPECT_NORTH'].started_at is not None


def test_fault_time_is_not_counted_as_route_wait():
    sim = Simulation(Scenario(id='wait', horizon_s=1))
    vid = 'TRACTOR_01'
    sim.world.route_wait[vid] = '前方车辆阻挡，等待通行'
    sim.world.vehicles[vid].state = VehicleState.DEGRADED
    sim._observe_step(sim.world, .5)
    assert sim.route_wait_s == 0


def test_scenario_roundtrip_and_invalid_schedules():
    scene = short_scenario()
    assert Scenario.model_validate_json(scene.model_dump_json()) == scene
    for values in ({'step_s': float('nan')}, {'step_s': 1e-10}, {'start_time': '2026-01-01'},
                   {'faults':[dict(id='bad',start_s=5,end_s=4,vehicle_id='TRACTOR_01',anomaly_type='communication_lost')]}):
        with pytest.raises(ValueError):
            short_scenario(**values)
    with pytest.raises(ValueError):
        Simulation(short_scenario(tasks=[dict(id='bad',type='inspection',origin='missing',destination='CHECK_N',deadline_s=5)]))
    with pytest.raises(ValueError):
        Simulation(scene).step(1e-10)


@pytest.fixture
def replay_payload(tmp_path):
    run_one(Scenario(id='replay', horizon_s=1), 0, 'priority_distance', tmp_path/'run')
    return json.loads((tmp_path/'run/replay.json').read_text())


@pytest.mark.parametrize('mutate', [
    lambda r:r.update(frames=[]),
    lambda r:r['frames'].append(r['frames'][0]),
    lambda r:r['layout']['nodes'][0].update(x=float('nan')),
    lambda r:r['layout']['nodes'][0].pop('type'),
    lambda r:r['layout']['edges'][0].update(to='missing'),
    lambda r:r['layout'].pop('edges'),
    lambda r:r['layout']['edges'][0].update(road_type='unknown'),
    lambda r:r['layout'].update(reference=None),
    lambda r:r['frames'][0]['vehicles'][0].update(current_speed_mps='bad'),
    lambda r:r['frames'][0]['crossings'][0].pop('aircraft'),
    lambda r:r['frames'][0].pop('simulation_time'),
    lambda r:r['manifest'].update(status='running'),
])
def test_replay_rejects_malformed_renderer_inputs(replay_payload, mutate):
    mutate(replay_payload)
    with pytest.raises(ValueError):
        load_replay(json.dumps(replay_payload).encode())


@pytest.mark.parametrize('content', [b'null', b'[]', b'{', b'"wrong"', b'\xff'])
def test_replay_invalid_json_is_reported(content):
    with pytest.raises(ValueError):
        load_replay(content)
