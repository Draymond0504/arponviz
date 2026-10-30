import json
from datetime import datetime, timedelta

import pytest

from apronops import checkpoint, facilities, kinematics, policies, recovery, simulator
from apronops.layout import Layout, Node
from apronops.models import Task, TaskType, TaskStatus, Vehicle, VehicleState, VehicleType

NOW = datetime(2026,1,1)


def depot_world(capacity=1):
    layout = Layout()
    layout.nodes = {nid:Node(nid,nid,kind,x,0.) for nid,kind,x in
                    [('D','depot',20),('A','road',0),('B','road',-20),('E','depot',-40)]}
    for a,b in [('D','A'),('A','B'),('B','E')]:
        layout.adjacency.setdefault(a,[]).append((b,20.,5.))
        layout.adjacency.setdefault(b,[]).append((a,20.,5.))
    resident = Vehicle(id='R',type=VehicleType.TRACTOR,node_id='D',x=20,y=0,speed_mps=4,last_heartbeat=NOW)
    incoming = resident.model_copy(update=dict(id='I',node_id='A',x=0,state=VehicleState.RETURNING,
                        target_node_id='D',task_phase='returning'))
    w = simulator.World(layout, {'R':resident,'I':incoming},{},now=NOW,seed=1,
        depot_config={'D':{'capacity':capacity,'pickup_s':10,'recovery_after_s':3},
                      'E':{'capacity':1,'pickup_s':10,'recovery_after_s':3}})
    return w


def assert_capacity(w):
    for nid in w.berths:
        occupied = list(w.berths[nid].values())+list(w.berth_reservations[nid].values())
        assert len(occupied) == len(set(occupied))
        assert len(occupied) <= w.facility_config[nid]['capacity']


def test_fifo_admission_and_parked_vehicle_not_on_road():
    w = depot_world(2)
    q = w.vehicles['I'].model_copy(update={'id':'Q'})
    w.vehicles[q.id] = q
    assert facilities.admit(w,w.vehicles['I'],'D')
    assert not facilities.admit(w,q,'D')
    assert not facilities.admit(w,q,'D')
    assert len(w.depot_queues['D']) == 1
    w.vehicles['I'].next_node_id = 'D'
    assert kinematics.sense_ahead_gap(w,w.vehicles['I']) is None
    facilities.depart(w,w.vehicles['R'])
    newcomer = q.model_copy(update={'id':'Z'})
    w.vehicles[newcomer.id] = newcomer
    assert not facilities.admit(w,newcomer,'D')
    assert facilities.admit(w,q,'D')
    assert_capacity(w)


def test_full_depot_recovery_physically_relocates_resident_and_admits_queue():
    w = depot_world()
    start = w.vehicles['R'].x
    for _ in range(240):
        previous = {v.id:(v.x,v.y) for v in w.vehicles.values()}
        simulator.advance(w,.5)
        for v in w.vehicles.values():
            assert abs(v.x-previous[v.id][0]) <= v.speed_mps*.5+1e-6
        assert_capacity(w)
        if w.vehicles['I'].node_id == 'D' and w.vehicles['R'].node_id == 'E':
            break
    assert w.vehicles['R'].x != start
    assert w.vehicles['I'].node_id == 'D' and w.vehicles['R'].node_id == 'E'
    assert w.home_node['R'] == 'D'
    assert w.recovery_counts['relocations'] == 1
    assert w.recovery_counts['resolved'] == 1
    assert w.recovery_counts['unresolved'] == 0
    assert w.distance_m['R'] == w.empty_distance_m['R']
    assert any(e.event_type == 'deadlock_relocation_arrived' for e in w.pending_events)


def test_faulted_resident_is_not_moved_and_queue_recovers_after_repair():
    w=depot_world()
    event=simulator.inject_anomaly(w,'communication_lost','R')
    alert=next(a for a in w.alerts.values() if a.event_id == event.id)
    simulator.advance(w,10)
    assert w.vehicles['R'].x==20 and w.vehicles['I'].x==0
    assert w.recovery_counts['relocations']==0 and w.recovery_counts['unresolved']==1
    assert facilities.forecast_wait(w,'D',w.now) is None
    simulator.resolve_alert(w,alert.id,'repaired')
    simulator.advance(w,100)
    assert w.vehicles['I'].node_id=='D'
    assert w.recovery_counts['resolved']==1


def test_closed_escape_route_leaves_blockage_visible_without_teleport():
    w=depot_world()
    simulator.close_road(w,'A','B','no escape')
    simulator.advance(w,20)
    assert w.vehicles['R'].x==20 and w.vehicles['I'].x==0
    assert not w.recovery_detours and w.recovery_counts['unresolved']==1


def test_queue_prediction_uses_remaining_handling_and_arrival_time():
    w=depot_world()
    resident=w.vehicles['R']
    resident.state=VehicleState.EN_ROUTE
    resident.task_phase='to_origin'
    w.pickup_remaining['R']=20
    assert facilities.forecast_wait(w,'D',w.now+timedelta(seconds=5),'I') == 15.5
    assert facilities.forecast_wait(w,'D',w.now+timedelta(seconds=30),'I') == 0
    resident.state=VehicleState.DEGRADED
    assert facilities.forecast_wait(w,'D',w.now+timedelta(seconds=10000),'I') is None


def test_known_service_queue_not_reported_as_deadlock():
    w=depot_world()
    resident=w.vehicles['R']
    resident.state=VehicleState.OPERATING
    resident.task_phase='servicing'
    resident.service_remaining_s=100
    task=Task(id='T',type=TaskType.BAGGAGE_TOW,origin='D',destination='D',created_at=NOW,
              status=TaskStatus.IN_PROGRESS,assigned_vehicle_id='R')
    resident.current_task_id='T';w.tasks[task.id]=task
    simulator.advance(w,20)
    assert w.recovery_counts['detected']==0
    assert w.depot_wait_s>0


def test_snapshot_in_middle_of_relocation_preserves_reservations_and_queue():
    w=depot_world()
    simulator.advance(w,6)
    assert w.recovery_detours
    payload=json.loads(json.dumps(checkpoint.dump_world(w)))
    other=checkpoint.restore_world(w.layout,payload)
    w.pending_events.clear()
    for instance in (w,other):
        simulator.advance(instance,70)
    assert checkpoint.dump_world(w)==checkpoint.dump_world(other)
    assert [e.model_dump() for e in w.pending_events]==[e.model_dump() for e in other.pending_events]


def test_pickup_handling_suspends_during_fault_then_completes_once():
    w=depot_world()
    w.vehicles.pop('I')
    task=Task(id='T',type=TaskType.BAGGAGE_TOW,origin='D',destination='B',created_at=NOW,
              deadline=NOW+timedelta(seconds=100),service_duration_s=2)
    w.tasks[task.id]=task
    simulator.advance(w,3)
    remaining=w.pickup_remaining['R']
    event=simulator.inject_anomaly(w,'position_quality_degraded','R')
    alert=next(a for a in w.alerts.values() if a.event_id==event.id)
    simulator.advance(w,20)
    assert w.pickup_remaining['R']==remaining and task.started_at is None
    simulator.resolve_alert(w,alert.id,'fixed')
    simulator.advance(w,80)
    assert task.status==TaskStatus.COMPLETED
    assert len([e for e in w.pending_events if e.event_type=='task_completed'])==1


def test_rolling_joint_matching_beats_greedy_pair_cost(monkeypatch):
    w=depot_world(2)
    for v in w.vehicles.values():
        v.state=VehicleState.IDLE;v.target_node_id=None;v.task_phase=None
    for tid in ('T1','T2'):
        w.tasks[tid]=Task(id=tid,type=TaskType.BAGGAGE_TOW,origin='D',destination='B',
                         created_at=NOW,deadline=NOW,priority=1)
    # Greedy T1->I costs 1, leaves T2->R costing 100; joint optimum is 2+2.
    costs={('I','T1'):1,('R','T1'):2,('I','T2'):2,('R','T2'):100}
    monkeypatch.setattr(policies,'estimate_finish',lambda world,v,t:NOW+timedelta(seconds=costs[v.id,t.id]))
    pairs=policies.rolling_assignments(w)
    assert {(t.id,v.id) for t,v in pairs}=={('T1','R'),('T2','I')}
    assert not policies.rolling_assignments(w)  # interval gates repeated decisions
    assert w.dispatch_stats['search_nodes']>1
    for t,v in pairs:simulator._start_assignment(w,v,t,w.now)
    w.now+=timedelta(seconds=15)
    assert not policies.rolling_assignments(w)  # no preemption


def test_reserved_depot_capacity_survives_fault_on_approach():
    w=depot_world(2)
    simulator.advance(w,1)
    assert 'I' in w.berth_reservations['D']
    simulator.inject_anomaly(w,'position_quality_degraded','I')
    simulator.advance(w,5)
    assert 'I' in w.berth_reservations['D']
    assert_capacity(w)


def test_capacity_cycle_is_detected_and_broken_without_losing_tasks():
    # Two full depots are adjacent; each resident needs the other's bay before
    # departure. A third depot provides an escape reservation.
    layout=Layout()
    layout.nodes={n:Node(n,n,'depot',x,y) for n,x,y in [('D',0,0),('E',20,0),('F',0,20)]}
    for a,b in [('D','E'),('D','F'),('E','F')]:
        layout.adjacency.setdefault(a,[]).append((b,20.,5.))
        layout.adjacency.setdefault(b,[]).append((a,20.,5.))
    vehicles={}
    tasks={}
    for vid,start,end in [('V1','D','E'),('V2','E','D')]:
        vehicles[vid]=Vehicle(id=vid,type=VehicleType.TRACTOR,node_id=start,x=layout.nodes[start].x,
            y=layout.nodes[start].y,speed_mps=4,last_heartbeat=NOW,state=VehicleState.OPERATING,
            task_phase='to_destination',target_node_id=end,current_task_id=vid)
        tasks[vid]=Task(id=vid,type=TaskType.BAGGAGE_TOW,origin=start,destination=end,created_at=NOW,
                       status=TaskStatus.IN_PROGRESS,assigned_vehicle_id=vid,service_duration_s=2)
    w=simulator.World(layout,vehicles,tasks,now=NOW,seed=1,
                      depot_config={n:{'capacity':1,'recovery_after_s':2} for n in layout.nodes})
    for _ in range(300):
        simulator.advance(w,.5)
        assert_capacity(w)
        if all(t.status==TaskStatus.COMPLETED for t in w.tasks.values()):break
    assert w.recovery_counts['cycles']>=1
    assert w.recovery_counts['relocations']>=1
    assert all(t.status==TaskStatus.COMPLETED for t in w.tasks.values())
    assert all(t.assigned_vehicle_id==t.id for t in w.tasks.values())
    assert not w.deadlock_cycles


def test_reroute_only_at_node_and_never_teleports_mid_edge():
    w=depot_world()
    w.layout.adjacency['A'].append(('E',40.,10.))
    w.layout.adjacency['E'].append(('A',40.,10.))
    v=w.vehicles['I'];v.target_node_id='E';v.next_node_id='B';v.edge_progress_m=1;v.x=-1
    assert not recovery._reroute(w,v)
    assert v.x==-1 and v.next_node_id=='B'
    v.edge_progress_m=0;v.x=0
    assert recovery._reroute(w,v)
    assert v.planned_path==['A','E'] and v.x==0


def test_config_and_legacy_capacity_migration():
    w=depot_world(2)
    with pytest.raises(ValueError):facilities.initialize(w,{'D':{'capacity':0}})
    w=depot_world(2)
    payload=json.loads(json.dumps(checkpoint.dump_world(w)))
    payload['version']=3;payload.pop('facilities');payload.pop('next_dispatch_at')
    payload['vehicles'] += [dict(payload['vehicles'][0],id='R2'),dict(payload['vehicles'][0],id='R3')]
    restored=checkpoint.restore_world(w.layout,payload)
    assert restored.facility_config['D']['capacity']==3
    assert len(restored.berths['D'])==3
    assert checkpoint.dump_world(restored)['version']==checkpoint.SNAPSHOT_VERSION


def test_rolling_replans_at_interval_after_snapshot_and_keeps_running_tasks():
    w=depot_world()
    w.policy_name='rolling_optimization'
    simulator.advance(w,1)
    restored=checkpoint.restore_world(w.layout,json.loads(json.dumps(checkpoint.dump_world(w))))
    assert restored.next_dispatch_at==w.next_dispatch_at
    assert list(policies.assignments(restored))==[]


def test_policy_switch_is_durable(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from apronops.api import main
    monkeypatch.setenv('APRONOPS_DB_PATH',str(tmp_path/'policy.db'))
    monkeypatch.setattr(main,'TICK_INTERVAL_S',3600.)
    with TestClient(main.app) as client:
        assert client.post('/dispatch/policy',params={'policy':'rolling_optimization'}).status_code==200
        state=client.get('/state').json()
    with TestClient(main.app) as client:
        assert client.get('/state').json()==state


def test_fault_on_waiter_does_not_mark_blockage_resolved():
    w=depot_world()
    simulator.close_road(w,'A','B','no escape')
    simulator.advance(w,5)
    assert w.recovery_counts['unresolved']==1
    simulator.inject_anomaly(w,'position_quality_degraded','I')
    simulator.advance(w,5)
    assert w.recovery_counts['resolved']==0
    assert w.recovery_counts['unresolved']==1


def test_legacy_mid_edge_arrival_gets_reserved_bay_without_rewind():
    w=depot_world(2)
    v=w.vehicles['I'];v.next_node_id='D';v.edge_progress_m=10;v.x=10
    payload=json.loads(json.dumps(checkpoint.dump_world(w)))
    payload['version']=3;payload.pop('facilities');payload.pop('next_dispatch_at')
    payload['vehicles'].append(dict(payload['vehicles'][0],id='R2'))
    restored=checkpoint.restore_world(w.layout,payload)
    assert restored.vehicles['I'].x==10 and restored.vehicles['I'].edge_progress_m==10
    assert restored.facility_config['D']['capacity']==3
    assert 'I' in restored.berth_reservations['D']
    simulator.advance(restored,20)
    assert restored.vehicles['I'].node_id=='D'
    assert_capacity(restored)
