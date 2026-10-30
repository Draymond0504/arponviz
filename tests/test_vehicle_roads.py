"""Vehicle roads stay separate from the aircraft graph and remain operable."""
from datetime import datetime
from pathlib import Path
import math

import pytest
import yaml

from apronops import simulator
from apronops.layout import Layout, Node, load_layout
from apronops.models import TaskStatus, Vehicle, VehicleState, VehicleType
from scripts.build_vehicle_layout import build, intersection

DATA = Path(__file__).resolve().parents[1] / 'data'


def test_generated_network_is_reproducible_and_all_work_points_are_reachable():
    raw = yaml.safe_load((DATA / 'apron_layout.yaml').read_text())
    assert build() == raw
    layout = load_layout(str(DATA / 'apron_layout.yaml'))
    assert len([n for n in layout.nodes.values() if n.type == 'service_point']) == 30
    assert not set(layout.nodes) & {n['id'] for n in layout.reference['nodes']}
    for nid in layout.nodes:
        assert layout.shortest_path('DEPOT_BAG_W', nid)[-1] == nid
    for e in raw['edges']:
        assert e['distance_m'] == pytest.approx(math.dist(layout.node_xy(e['from']), layout.node_xy(e['to'])), abs=1e-5)
        assert e['speed_limit_mps'] > 0
        assert layout.speed_limit(e['from'], e['to']) == layout.speed_limit(e['to'], e['from'])


def test_aircraft_intersections_are_explicit_and_do_not_cut_stand_access():
    layout = load_layout(str(DATA / 'apron_layout.yaml'))
    ref = {n['id']: n for n in layout.reference['nodes']}
    raw = yaml.safe_load((DATA / 'apron_layout.yaml').read_text())
    found = set()
    for edge in raw['edges']:
        a, b = layout.node_xy(edge['from']), layout.node_xy(edge['to'])
        for aircraft_edge in layout.reference['edges']:
            c, d = ref[aircraft_edge['from']], ref[aircraft_edge['to']]
            p = intersection(a, b, (c['x'], c['y']), (d['x'], d['y']))
            if p is None:
                continue
            assert c['type'] == d['type'] == 'taxiway'
            endpoints = [layout.nodes[edge[k]] for k in ('from', 'to')]
            assert any(n.type == 'crossing' and math.dist((n.x, n.y), p) < 1e-4 for n in endpoints)
            found.add(p)
    assert len(found) == 11


def test_road_speed_cap_and_unoccupied_crossing_passage():
    layout = Layout()
    layout.nodes = {key: Node(key, key, 'crossing' if key == 'B' else 'road', x, 0.)
                    for key, x in [('A', 0.), ('B', 10.), ('C', 20.)]}
    layout.adjacency = {'A': [('B', 10., 10.)], 'B': [('A', 10., 10.), ('C', 10., 10.)], 'C': [('B', 10., 10.)]}
    layout.edge_attributes = {('A', 'B'): {'speed_limit_mps': 1.0}, ('B', 'C'): {'speed_limit_mps': 1.0}}
    layout.metadata['crossing_wait_s'] = 3.0
    vehicle = Vehicle(id='V', type=VehicleType.TRACTOR, node_id='A', target_node_id='C',
                      x=0., y=0., speed_mps=6., last_heartbeat=datetime.utcnow())
    world = simulator.World(layout, {'V': vehicle}, {})
    for _ in range(200):
        simulator._move_along_path(world, vehicle, 0.1)
        assert vehicle.current_speed_mps <= 1.0
        if vehicle.node_id == 'B':
            break
    assert vehicle.node_id == 'B'
    assert world.crossing_wait.get('V', 0.) == 0.
    simulator._move_along_path(world, vehicle, 1.)
    assert vehicle.x > 10.


def test_seed_tasks_complete_and_vehicles_return_to_dedicated_bases(monkeypatch):
    monkeypatch.setattr(simulator, '_persist', lambda world: None)
    world = simulator.load_world()
    for _ in range(600):
        simulator.tick(world, dt=5., random_anomaly_prob=0.)
        if all(t.status == TaskStatus.COMPLETED for t in world.tasks.values()) and all(v.state == VehicleState.IDLE for v in world.vehicles.values()):
            break
    assert all(t.status == TaskStatus.COMPLETED for t in world.tasks.values())
    assert all(v.state == VehicleState.IDLE and v.node_id == world.home_node[v.id] for v in world.vehicles.values())


def test_route_api_rejects_aircraft_node_and_returns_vehicle_path(monkeypatch):
    from fastapi import HTTPException
    from apronops.api import main
    world = simulator.load_world()
    monkeypatch.setattr(main, '_world', world)
    route = main.get_route('DEPOT_CATER', 'SP_20')
    assert route['distance_m'] > 0 and route['crossings']
    assert all(n in world.layout.nodes for n in route['path'])
    with pytest.raises(HTTPException) as error:
        main.get_route('900', 'SP_20')
    assert error.value.status_code == 400
