"""reset / step engine. No API, wall-clock sleep, UI, or database calls."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import math

from .. import aircraft, crossings, facilities, flights, simulator
from ..checkpoint import dump_world
from ..models import Aircraft, CrossingOccupancy, Flight, Task, TaskStatus, TaskType, VehicleState
from ..policies import POLICIES
from .scenario import Scenario, realize


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Simulation:
    def __init__(self, scenario: Scenario, seed=0, policy='priority_distance', data_dir=simulator.DATA_DIR):
        if policy not in POLICIES:
            raise ValueError(f'unknown policy {policy}')
        self.scenario, self.seed, self.policy, self.data_dir = scenario, seed, policy, data_dir
        self.reset()

    def reset(self):
        self.world = simulator.load_world(self.data_dir, now=self.scenario.start_time, seed=self.seed,
            vehicle_seed=[v.model_dump() for v in self.scenario.vehicles] if self.scenario.vehicles is not None else None,
            depot_overrides={k:v.model_dump() for k,v in self.scenario.depots.items()})
        self.world.tasks.clear()  # Experiments never inherit live seed work or DB state.
        self.world.policy_name = self.policy
        self.events = realize(self.scenario, self.seed)
        self.cursor = 0
        self.handles = {}
        self.crossing_wait_s = 0.
        self.route_wait_s = 0.
        self.traffic_wait_s = 0.
        self.violation_steps = 0
        self.capacity_violation_steps = 0
        self.elapsed_s = 0.
        self._validate_references()
        # Exclude the policy from input hash, allowing paired-policy verification.
        self.initial_world = json.loads(json.dumps(dump_world(self.world)))
        initial = dict(self.initial_world)
        initial.pop('policy_name')
        self.input_hash = digest({'initial': initial, 'events': self.events})
        self._apply_due()
        return self.observe()

    def _validate_references(self):
        layout = self.world.layout
        for c in self.scenario.crossings:
            if c.crossing_id not in layout.nodes or layout.nodes[c.crossing_id].type != 'crossing':
                raise ValueError(f'unknown crossing {c.crossing_id}')
        for c in self.scenario.closures:
            layout.edge_distance(c.from_node, c.to_node)
        for f in self.scenario.faults:
            if f.vehicle_id not in self.world.vehicles:
                raise ValueError(f'unknown vehicle {f.vehicle_id}')
        for f in self.scenario.flights:
            if f.stand_id not in layout.nodes or layout.nodes[f.stand_id].type != 'service_point':
                raise ValueError(f'unknown stand {f.stand_id}')
        for a in self.scenario.aircraft:
            g=aircraft.graph(self.world)
            if a.entry_node not in g.nodes or g.nodes[a.entry_node].type!='entry':
                raise ValueError('invalid aircraft entry')
            if a.exit_node not in g.nodes or g.nodes[a.exit_node].type!='exit':
                raise ValueError('invalid aircraft exit')
            flight=next(f for f in self.scenario.flights if f.id==a.flight_id)
            stand=str(int(flight.stand_id.removeprefix('SP_'))-1)
            g.shortest_path(a.entry_node,stand)
            g.shortest_path(stand,a.exit_node)
        flight_task_ids = {f'FLIGHT:{f.id}:{kind}' for f in self.scenario.flights for kind in ('baggage','catering','inspection')}
        for t in self.scenario.tasks:
            if t.id in flight_task_ids:
                raise ValueError('manual task ID overlaps generated flight task')
            for nid in (t.origin, t.destination):
                if nid not in layout.nodes or layout.nodes[nid].type == 'crossing':
                    raise ValueError(f'invalid task endpoint {nid}')

    def _apply_due(self):
        world = self.world
        date = lambda seconds: self.scenario.start_time+timedelta(seconds=seconds)
        while self.cursor < len(self.events) and self.events[self.cursor]['at_s'] <= self.elapsed_s+1e-7:
            event = self.events[self.cursor]
            p, kind, eid = event['payload'], event['kind'], event['id']
            if kind == 'flight':
                flights.upsert_flight(world, Flight(id=eid, stand_id=p['stand_id'], arrival_at=date(p['arrival_s']),
                                                  departure_at=date(p['arrival_s']+p['turnaround_s'])))
            elif kind == 'flight_aircraft':
                aircraft.register(world, Aircraft(**{k:v for k,v in p.items() if k!='taxi_start_s'},
                    taxi_start_at=date(p['taxi_start_s'])))
            elif kind == 'task':
                world.tasks[eid] = Task(id=eid, type=TaskType(p['type']), origin=p['origin'], destination=p['destination'],
                    priority=p['priority'], created_at=world.now, release_at=date(p['release_s']),
                    deadline=date(p['deadline_s']), service_duration_s=p['service_duration_s'])
                simulator.record_event(world, 'task_created', 'scenario', eid)
            elif kind == 'crossing':
                crossings.register_occupancy(world, CrossingOccupancy(id=eid, crossing_id=p['crossing_id'],
                    aircraft_id=p['aircraft_id'], predicted_start=date(p['predicted_start_s']),
                    predicted_end=date(p['predicted_start_s']+p['duration_s']), requested_start=date(p['requested_start_s']),
                    requested_end=date(p['requested_start_s']+p['duration_s'])))
            elif kind == 'closure':
                self.handles[eid, kind] = simulator.close_road(world, p['from_node'], p['to_node'], 'scenario:'+eid)['id']
            elif kind == 'closure_end':
                simulator.reopen_road(world, self.handles[eid, 'closure'], 'scheduled recovery')
            elif kind == 'fault':
                result = simulator.inject_anomaly(world, p['anomaly_type'], p['vehicle_id'])
                self.handles[eid, kind] = next(a.id for a in world.alerts.values() if a.event_id == result.id)
            elif kind == 'fault_end':
                simulator.resolve_alert(world, self.handles[eid, 'fault'], 'scheduled recovery')
            self.cursor += 1
        crossings.update_crossings(world)

    def _observe_step(self, world, dt):
        for nid, config in world.facility_config.items():
            slots = list(world.berths[nid].values())+list(world.berth_reservations[nid].values())
            if len(slots) > config['capacity'] or len(set(slots)) != len(slots):
                self.capacity_violation_steps += 1
        for vid, reason in world.route_wait.items():
            if world.vehicles[vid].state not in (VehicleState.EN_ROUTE, VehicleState.OPERATING, VehicleState.RETURNING):
                continue  # A stale route reason during a fault is not route waiting.
            self.route_wait_s += dt
            if '飞机' in reason or '交叉区' in reason:
                self.crossing_wait_s += dt
            if '前方车辆' in reason:
                self.traffic_wait_s += dt
        for row in crossings.crossing_rows(world):
            if (row['aircraft'] and row['vehicles']) or len(row['aircraft']) > 1 or len(row['vehicles']) > 1:
                self.violation_steps += 1

    def step(self, dt=None):
        dt = self.scenario.step_s if dt is None else dt
        if not math.isfinite(dt) or dt < 1e-6:
            raise ValueError('step duration must be finite and at least one microsecond')
        dt = round(dt, 6)
        target = min(self.scenario.horizon_s, self.elapsed_s+dt)
        while self.elapsed_s < target-1e-7:
            self._apply_due()
            next_event = self.events[self.cursor]['at_s'] if self.cursor < len(self.events) else target
            stop = min(target, self.elapsed_s+self.scenario.step_s, next_event)
            simulator.advance(self.world, stop-self.elapsed_s, observer=self._observe_step)
            self.elapsed_s = round((self.world.now-self.scenario.start_time).total_seconds(), 6)
        self._apply_due()
        return self.observe()

    def drain_events(self):
        events = [e.model_dump(mode='json') for e in self.world.pending_events]
        self.world.pending_events.clear()
        return events

    def observe(self):
        w = self.world
        return {'elapsed_s': self.elapsed_s, 'simulation_time': w.now.isoformat(),
                'aircraft':aircraft.rows(w),
                'depots':facilities.rows(w), 'recovery':dict(w.recovery_counts),
                'wait_for':{k:list(v) for k,v in w.wait_for.items()}, 'deadlock_cycles':[list(c) for c in w.deadlock_cycles],
                'vehicles': [{**v.model_dump(mode='json'), 'wait_reason': w.route_wait.get(v.id, ''),
                             'recovery_action':facilities.recovery_action(w,v.id)} for v in w.vehicles.values()],
                'tasks': [{k: t.model_dump(mode='json')[k] for k in ('id','flight_id','type','status','assigned_vehicle_id','origin','destination','deadline','completed_at')}
                          for t in w.tasks.values()],
                'crossings': crossings.crossing_rows(w), 'closures': [dict(c) for c in w.closures.values()]}

    def summary(self, status='completed'):
        w = self.world
        released = [t for t in w.tasks.values() if t.release_at is None or t.release_at <= w.now]
        completed = [t for t in released if t.status == TaskStatus.COMPLETED]
        due = [t for t in released if t.deadline and t.deadline <= w.now]
        on_time = [t for t in due if t.completed_at and t.completed_at <= t.deadline]
        tardiness = sorted(max(0., ((t.completed_at or w.now)-t.deadline).total_seconds()) for t in due)
        completed_tardiness = sorted(max(0., (t.completed_at-t.deadline).total_seconds()) for t in completed if t.deadline)
        granted = [r for r in w.occupancies.values() if r.actual_start is not None]
        return {'status': status, 'elapsed_s': self.elapsed_s, 'horizon_s': self.scenario.horizon_s,
                'task_total': len(w.tasks), 'released_tasks': len(released), 'completed_tasks': len(completed),
                'unfinished_tasks': len(released)-len(completed), 'unreleased_tasks': len(w.tasks)-len(released),
                'remaining_input_events': len(self.events)-self.cursor,
                'completion_rate': len(completed)/len(released) if released else None,
                'due_tasks': len(due), 'due_on_time_rate': len(on_time)/len(due) if due else None,
                'tardiness_lower_bound_s': sum(tardiness),
                'completed_tardiness_p95_s': completed_tardiness[max(0, math.ceil(.95*len(completed_tardiness))-1)] if completed_tardiness else None,
                'distance_m': sum(w.distance_m.values()), 'empty_distance_m': sum(w.empty_distance_m.values()),
                'crossing_wait_s': self.crossing_wait_s, 'route_wait_s': self.route_wait_s,
                'traffic_wait_s': self.traffic_wait_s,
                'depot_wait_s':w.depot_wait_s, 'deadlocks_detected':w.recovery_counts['detected'],
                'deadlocks_resolved':w.recovery_counts['resolved'], 'deadlocks_unresolved':w.recovery_counts['unresolved'],
                'depot_deadlock_cycles':w.recovery_counts['cycles'],
                'depot_capacity_violation_steps':self.capacity_violation_steps,
                'recovery_relocations':w.recovery_counts['relocations'], 'recovery_reroutes':w.recovery_counts['rerouted'],
                'rolling_epochs':w.dispatch_stats['epochs'],
                'conflict_zone_violation_steps': self.violation_steps,
                'aircraft_grant_delay_s': sum((r.actual_start-r.requested_start).total_seconds() for r in granted),
                'aircraft_ungranted_requests': sum(r.requested_start <= w.now and r.actual_start is None for r in w.occupancies.values()),
                'aircraft_total':len(w.aircraft),
                'aircraft_arrived':sum(a.actual_arrival_at is not None for a in w.aircraft.values()),
                'aircraft_departed':sum(a.state=='departed' for a in w.aircraft.values()),
                'aircraft_wait_s':sum(a.wait_s for a in w.aircraft.values()),
                'aircraft_distance_m':sum(a.distance_m for a in w.aircraft.values()),
                'aircraft_departure_delay_lower_bound_s':sum(max(0.,((a.actual_departure_at or w.now)-w.flights[a.flight_id].departure_at).total_seconds()) for a in w.aircraft.values())}
