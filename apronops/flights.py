"""Synthetic turnaround templates and idempotent flight revisions."""
from __future__ import annotations

from datetime import timedelta

from .models import Flight, Task, TaskStatus, TaskType


def upsert_flight(world, flight: Flight, expected_revision: int | None = None) -> Flight:
    from .simulator import record_event

    if flight.arrival_at >= flight.departure_at:
        raise ValueError('离位时间必须晚于到位时间')
    node = world.layout.nodes.get(flight.stand_id)
    if node is None or node.type != 'service_point':
        raise ValueError('航班机位必须是有效的机位服务点')
    old = world.flights.get(flight.id)
    if old and old.stand_id != flight.stand_id and any(a.flight_id==flight.id for a in world.aircraft.values()):
        raise ValueError('已关联航空器，不能直接更换机位')
    if old:
        if (old.stand_id, old.arrival_at, old.departure_at) == (flight.stand_id, flight.arrival_at, flight.departure_at):
            return old
        if expected_revision != old.revision:
            raise ValueError('航班版本已变化，请刷新后重新提交')
    elif expected_revision is not None:
        raise ValueError('航班不存在，不能按已有版本更新')
    existing = [t for t in world.tasks.values() if t.flight_id == flight.id]
    if old and old.stand_id != flight.stand_id and any(t.status != TaskStatus.PENDING for t in existing):
        raise ValueError('已有任务开始执行或发生转派，不能直接更换机位')
    if old and all(t.status == TaskStatus.COMPLETED for t in existing):
        raise ValueError('该航班保障已完成，不能再修改运行计划')
    # The inspection is a synthetic post-service check, not a universal airport SOP.
    origin = 'DEPOT_BAG_W' if node.x < 0 else 'DEPOT_BAG_E'
    prefix = f'FLIGHT:{flight.id}:'
    specs = [('baggage', TaskType.BAGGAGE_TOW, origin, 480., [], 4),
             ('catering', TaskType.CATERING_SUPPLY, 'DEPOT_CATER', 600., [], 3),
             ('inspection', TaskType.INSPECTION, 'DEPOT_AMR', 180.,
              [prefix + 'baggage', prefix + 'catering'], 4)]
    generated = []
    for key, kind, origin, duration, dependencies, priority in specs:
        if origin not in world.layout.nodes:
            raise ValueError(f'缺少任务模板所需基地 {origin}')
        task_id = prefix + key
        task = world.tasks.get(task_id)
        if task is not None and task.flight_id != flight.id:
            raise ValueError(f'任务 ID 冲突：{task_id}')
        if task is None:
            task = Task(id=task_id, type=kind, origin=origin, destination=flight.stand_id,
                        created_at=world.now, priority=priority, flight_id=flight.id,
                        release_at=flight.arrival_at, deadline=flight.departure_at,
                        service_duration_s=duration, depends_on=dependencies)
        else:
            task = task.model_copy(deep=True)
            if task.status == TaskStatus.COMPLETED:
                generated.append(task)
                continue  # Preserve original completed-task SLA history.
            task.deadline = flight.departure_at
            if task.status in (TaskStatus.PENDING, TaskStatus.REASSIGNED):
                task.release_at = flight.arrival_at
                task.destination = flight.stand_id
                task.origin = origin
        generated.append(task)
    flight = flight.model_copy(update={'revision': old.revision + 1 if old else 1})
    world.flights[flight.id] = flight
    for task in generated:
        is_new = task.id not in world.tasks
        world.tasks[task.id] = task
        if is_new:
            record_event(world, 'task_created', f'flight:{flight.id}', task.id, template=flight.template)
    record_event(world, 'flight_updated' if old else 'flight_created', f'flight:{flight.id}',
                 flight=flight.model_dump(mode='json'), previous=old.model_dump(mode='json') if old else None)
    return flight


def flight_rows(world):
    rows = []
    for flight in world.flights.values():
        tasks = [t for t in world.tasks.values() if t.flight_id == flight.id]
        completed = sum(t.status == TaskStatus.COMPLETED for t in tasks)
        status = ('保障完成' if tasks and completed == len(tasks) else
                  '保障逾期' if world.now >= flight.departure_at else
                  '等待到位' if world.now < flight.arrival_at else '保障中')
        plane = next((a for a in world.aircraft.values() if a.flight_id==flight.id),None)
        if plane:
            status = {'scheduled':'等待航空器进场','taxi_in':'进场滑行','parked':status,
                      'taxi_out':'离位滑行','departed':'已离场'}[plane.state]
        rows.append({**flight.model_dump(mode='json'), 'status': status,
                     'aircraft_id':plane.id if plane else None,
                     'actual_arrival_at':plane.actual_arrival_at.isoformat() if plane and plane.actual_arrival_at else None,
                     'actual_departure_at':plane.actual_departure_at.isoformat() if plane and plane.actual_departure_at else None,
                     'completed_tasks': completed, 'total_tasks': len(tasks)})
    return rows


def load_demo(world):
    """Explicit, repeat-safe scenario action; never injected on normal startup."""
    from .crossings import register_occupancy
    from .models import CrossingOccupancy

    if 'DEMO_TURNAROUND_01' in world.flights:
        return world.flights['DEMO_TURNAROUND_01']
    flight = upsert_flight(world, Flight(id='DEMO_TURNAROUND_01', stand_id='SP_20',
                          arrival_at=world.now+timedelta(seconds=20),
                          departure_at=world.now+timedelta(minutes=45)))
    for i, cid in enumerate(('X_11', 'X_10')):
        start = world.now+timedelta(seconds=130+60*i)
        register_occupancy(world, CrossingOccupancy(id=f'DEMO_CROSSING_{i+1}', crossing_id=cid,
                          aircraft_id='DEMO_TAXI_01', predicted_start=start,
                          predicted_end=start+timedelta(seconds=60), requested_start=start+timedelta(seconds=20),
                          requested_end=start+timedelta(seconds=80)))
    return flight


def load_aircraft_demo(world):
    from .aircraft import register
    from .models import Aircraft
    fid='DEMO_AIRCRAFT_01'
    if fid in world.flights:
        return world.flights[fid]
    flight=upsert_flight(world,Flight(id=fid,stand_id='SP_07',arrival_at=world.now+timedelta(seconds=120),
                                    departure_at=world.now+timedelta(minutes=45)))
    register(world,Aircraft(id='DEMO_AC_01',flight_id=fid,taxi_start_at=world.now+timedelta(seconds=10)))
    return flight
