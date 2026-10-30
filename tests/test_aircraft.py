from datetime import datetime, timedelta
import json

import pytest

from apronops import aircraft, checkpoint, crossings, policies, simulator
from apronops.experiments.engine import Simulation
from apronops.experiments.scenario import Scenario
from apronops.models import Aircraft, Flight, TaskStatus, VehicleState
from apronops.flights import upsert_flight


def setup_world():
    w=simulator.load_world(now=datetime(2026,1,1),seed=1)
    w.tasks.clear()
    upsert_flight(w,Flight(id='F',stand_id='SP_07',arrival_at=w.now,departure_at=w.now+timedelta(seconds=500)))
    a=aircraft.register(w,Aircraft(id='A',flight_id='F',taxi_start_at=w.now))
    return w,a


def test_aircraft_actual_arrival_gates_tasks_and_full_turnaround():
    w,a=setup_world()
    assert not policies.task_order(w)
    for t in w.tasks.values():t.service_duration_s=1
    simulator.advance(w,60)
    assert a.actual_arrival_at is not None
    assert a.distance_m>0 and a.state=='parked'
    for _ in range(800):
        simulator.advance(w,5)
        if a.actual_departure_at:
            assert all(t.status==TaskStatus.COMPLETED for t in w.tasks.values())
        if a.state=='departed':break
    assert a.state=='departed'
    assert a.actual_arrival_at<a.actual_departure_at<a.actual_exit_at
    assert a.node_id=='901'
    assert a.actual_departure_at>=w.flights['F'].departure_at


def test_aircraft_holds_for_vehicle_and_physical_ownership_survives_expiry_restart():
    w,a=setup_world()
    aircraft.prepare(w)
    _,_,cid=aircraft.route_zones(w,a)[0]
    v=w.vehicles['TRACTOR_01']
    v.node_id=cid;v.next_node_id=None;v.x,v.y=w.layout.node_xy(cid)
    v.state=VehicleState.DEGRADED
    simulator.advance(w,100)
    request=w.occupancies[next(iter(a.crossing_handles.values()))]
    assert request.status=='waiting_clearance' and a.actual_arrival_at is None
    held=(a.x,a.y)
    simulator.advance(w,5)
    assert (a.x,a.y)==held
    v.state=VehicleState.RETURNING;v.task_phase='returning';v.target_node_id=w.home_node[v.id];v.last_heartbeat=w.now
    for _ in range(200):
        simulator.advance(w,.5)
        if request.status=='active':break
    assert request.status=='active'
    assert not crossings.vehicle_in_zone(w,v,cid)
    restored=checkpoint.restore_world(w.layout,json.loads(json.dumps(checkpoint.dump_world(w))))
    assert restored.aircraft['A'].route_progress_m==a.route_progress_m
    assert restored.aircraft_taxi_owner=='A'
    restored.now=request.actual_end+timedelta(seconds=100)
    crossings.update_crossings(restored)
    assert restored.occupancies[request.id].status=='active'
    # A forecast expiry alone cannot release the aircraft's physical token.
    assert crossings.admission_reason(restored,v,cid,restored.now+timedelta(seconds=1))


def test_same_stand_and_network_are_reserved_and_revisions_are_guarded():
    w,a=setup_world()
    upsert_flight(w,Flight(id='F2',stand_id='SP_07',arrival_at=w.now,departure_at=w.now+timedelta(seconds=700)))
    b=aircraft.register(w,Aircraft(id='B',flight_id='F2',taxi_start_at=w.now))
    simulator.advance(w,100)
    assert a.state=='parked' and b.state=='scheduled' and b.wait_reason=='等待机位释放'
    with pytest.raises(ValueError,match='机位'):
        upsert_flight(w,w.flights['F'].model_copy(update={'stand_id':'SP_20'}),1)
    with pytest.raises(ValueError,match='已经关联'):
        aircraft.register(w,Aircraft(id='C',flight_id='F',taxi_start_at=w.now))


def test_aircraft_snapshot_continuation_and_legacy_snapshot():
    w,a=setup_world()
    simulator.advance(w,42)
    saved=json.loads(json.dumps(checkpoint.dump_world(w)))
    restored=checkpoint.restore_world(w.layout,saved)
    for world in (w,restored):
        world.pending_events.clear();simulator.advance(world,7)
    assert checkpoint.dump_world(w)==checkpoint.dump_world(restored)
    saved.update(version=4,aircraft=[],aircraft_taxi_owner=None)
    assert not checkpoint.restore_world(w.layout,saved).aircraft
    damaged=json.loads(json.dumps(checkpoint.dump_world(w)))
    damaged['aircraft_layout_fingerprint']='wrong'
    with pytest.raises(ValueError,match='Aircraft layout'):
        checkpoint.restore_world(w.layout,damaged)


def test_aircraft_scenario_and_fleet_override_are_paired():
    spec=Scenario(id='air',horizon_s=100,flights=[dict(id='F',stand_id='SP_07',arrival_s=50,turnaround_s=200)],
        aircraft=[dict(id='A',flight_id='F',taxi_start_s=0)],
        vehicles=[dict(id='C',type='catering',home_node='DEPOT_CATER',speed_mps=3)])
    sims=[Simulation(spec,1,p) for p in policies.POLICIES]
    assert len({s.input_hash for s in sims})==1
    assert list(sims[0].world.vehicles)==['C']
    assert 'A' in sims[0].world.aircraft
    assert Scenario.model_validate_json(spec.model_dump_json())==spec
    invalid=spec.model_copy(deep=True)
    invalid.aircraft[0].entry_node='100'
    invalid.flights[0].announce_s=20
    invalid.aircraft[0].taxi_start_s=30
    with pytest.raises(ValueError,match='entry'):
        Simulation(invalid)
