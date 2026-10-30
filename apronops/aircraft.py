"""Aircraft taxi motion on the independent reference graph.

The first traffic rule reserves the entire taxi network for one moving aircraft.
Parked stands remain separate resources. Physical crossing occupancy is released
by tail clearance, never by the estimated expiry of a synthetic time window.
"""
from __future__ import annotations

from datetime import timedelta
import math

from .layout import Layout, Node
from .models import Aircraft, CrossingOccupancy, TaskStatus


def graph(world):
    if getattr(world, '_aircraft_graph', None) is None:
        raw = world.layout.reference
        g = Layout()
        g.nodes = {n['id']: Node(**{k:n[k] for k in ('id','label','type','x','y')}) for n in raw.get('nodes', [])}
        for e in raw.get('edges', []):
            a,b=e['from'],e['to']
            distance=math.dist(g.node_xy(a),g.node_xy(b))
            if distance <= 0:
                raise ValueError('航空器路段长度必须为正')
            g.adjacency.setdefault(a,[]).append((b,distance,distance/4))
            g.adjacency.setdefault(b,[]).append((a,distance,distance/4))
        world._aircraft_graph=g
    return world._aircraft_graph


def stand_node(world, flight_id):
    return str(int(world.flights[flight_id].stand_id.removeprefix('SP_'))-1)


def register(world, plane):
    from .simulator import record_event
    if plane.id in world.aircraft:
        raise ValueError('航空器 ID 已存在')
    if plane.flight_id not in world.flights:
        raise ValueError('请先创建对应航班')
    if any(a.flight_id==plane.flight_id for a in world.aircraft.values()):
        raise ValueError('该航班已经关联航空器')
    if plane.taxi_start_at < world.now:
        raise ValueError('不能向过去插入航空器进场计划')
    tasks=[t for t in world.tasks.values() if t.flight_id==plane.flight_id]
    if any(t.status!=TaskStatus.PENDING for t in tasks):
        raise ValueError('保障任务已开始，不能事后添加航空器')
    g=graph(world)
    if g.nodes.get(plane.entry_node) is None or g.nodes[plane.entry_node].type!='entry':
        raise ValueError('航空器入口必须是 entry 节点')
    if g.nodes.get(plane.exit_node) is None or g.nodes[plane.exit_node].type!='exit':
        raise ValueError('航空器出口必须是 exit 节点')
    stand=stand_node(world,plane.flight_id)
    g.shortest_path(plane.entry_node,stand)
    g.shortest_path(stand,plane.exit_node)
    plane=plane.model_copy(deep=True)
    plane.node_id=plane.entry_node
    plane.x,plane.y=g.node_xy(plane.entry_node)
    world.aircraft[plane.id]=plane
    record_event(world,'aircraft_scheduled',plane.id,flight_id=plane.flight_id)
    return plane


def task_ready(world, task):
    return not any(a.flight_id==task.flight_id and a.actual_arrival_at is None
                   for a in world.aircraft.values()) if task.flight_id else True


def route_zones(world, plane):
    """Arc-length intervals of the plane centre with tail/nose margin at crossings."""
    cache=getattr(world,'_aircraft_zone_cache',{})
    key=(tuple(plane.route),plane.length_m)
    if key in cache:
        return cache[key]
    g=graph(world); offset=0.; zones=[]
    for a,b in zip(plane.route,plane.route[1:]):
        ax,ay=g.node_xy(a); bx,by=g.node_xy(b)
        dx,dy=bx-ax,by-ay; length=math.hypot(dx,dy)
        for n in world.layout.nodes.values():
            if n.type!='crossing':
                continue
            fraction=((n.x-ax)*dx+(n.y-ay)*dy)/(length*length)
            if -.00001<=fraction<=1.00001 and math.hypot(ax+fraction*dx-n.x,ay+fraction*dy-n.y)<.001:
                centre=offset+max(0.,min(1.,fraction))*length
                margin=plane.length_m/2+3.
                zones.append((max(0.,centre-margin),centre+margin,n.id))
        offset+=length
    # One crossing can occur exactly at an aircraft graph vertex.
    unique={cid:(start,min(end,offset),cid) for start,end,cid in zones}
    result=sorted(unique.values())
    cache[key]=result;world._aircraft_zone_cache=cache
    return result


def _position(world, plane):
    g=graph(world); remain=plane.route_progress_m
    for a,b in zip(plane.route,plane.route[1:]):
        length=g.edge_distance(a,b)
        if remain <= length+1e-8:
            ax,ay=g.node_xy(a); bx,by=g.node_xy(b)
            fraction=min(1.,remain/length)
            plane.node_id=a;plane.next_node_id=b
            plane.x=ax+(bx-ax)*fraction;plane.y=ay+(by-ay)*fraction
            plane.heading_deg=math.degrees(math.atan2(by-ay,bx-ax))
            return
        remain-=length
    plane.node_id=plane.route[-1];plane.next_node_id=None
    plane.x,plane.y=g.node_xy(plane.node_id)


def _start_leg(world, plane, outbound=False):
    from .simulator import record_event
    stand=stand_node(world,plane.flight_id)
    plane.route=graph(world).shortest_path(stand if outbound else plane.entry_node,
                                         plane.exit_node if outbound else stand)
    plane.route_progress_m=0.;plane.crossing_handles={}
    plane.state='taxi_out' if outbound else 'taxi_in'
    if outbound:
        plane.actual_departure_at=world.now
    world.aircraft_taxi_owner=plane.id
    _position(world,plane)
    record_event(world,'aircraft_taxi_out' if outbound else 'aircraft_taxi_in',plane.id,flight_id=plane.flight_id,route=plane.route)


def prepare(world):
    from . import crossings
    for plane in sorted(world.aircraft.values(),key=lambda a:(a.taxi_start_at,a.id)):
        if plane.state=='departed':
            continue
        plane.wait_reason=''
        if plane.state=='scheduled':
            if world.now < plane.taxi_start_at:
                continue
            occupied=any(a.id!=plane.id and a.state not in ('scheduled','departed')
                         and world.flights[a.flight_id].stand_id==world.flights[plane.flight_id].stand_id
                         for a in world.aircraft.values())
            if occupied:
                plane.wait_reason='等待机位释放';continue
            if world.aircraft_taxi_owner:
                plane.wait_reason='等待滑行放行';continue
            _start_leg(world,plane)
        if plane.state=='parked':
            flight=world.flights[plane.flight_id]
            if any(t.flight_id==plane.flight_id and t.status!=TaskStatus.COMPLETED for t in world.tasks.values()):
                plane.wait_reason='等待保障任务完成';continue
            if world.now < flight.departure_at:
                plane.wait_reason='等待计划离位时间';continue
            sp=world.layout.nodes[flight.stand_id]
            if any(math.hypot(v.x-sp.x,v.y-sp.y)<12 for v in world.vehicles.values()):
                plane.wait_reason='等待机位车辆撤离';continue
            if world.aircraft_taxi_owner:
                plane.wait_reason='等待滑行放行';continue
            _start_leg(world,plane,True)
        if plane.state in ('taxi_in','taxi_out'):
            for start,end,cid in route_zones(world,plane):
                if start-1e-7 <= plane.route_progress_m < end-1e-7 and cid not in plane.crossing_handles:
                    duration=(end-start)/plane.speed_mps
                    request=CrossingOccupancy(id=f'AIR:{plane.id}:{plane.state}:{cid}',crossing_id=cid,
                        aircraft_id=plane.id,predicted_start=world.now,predicted_end=world.now+timedelta(seconds=duration),
                        requested_start=world.now,requested_end=world.now+timedelta(seconds=duration),source='aircraft_motion')
                    crossings.register_occupancy(world,request)
                    plane.crossing_handles[cid]=request.id


def advance(world, dt):
    from .simulator import record_event
    for plane in world.aircraft.values():
        plane.current_speed_mps=0.
        if plane.state not in ('taxi_in','taxi_out'):
            if plane.wait_reason and plane.wait_reason!='等待计划离位时间':
                plane.wait_s+=dt
            continue
        g=graph(world)
        total=sum(g.edge_distance(a,b) for a,b in zip(plane.route,plane.route[1:]))
        target=min(total,plane.route_progress_m+plane.speed_mps*dt)
        zones=route_zones(world,plane)
        for start,end,cid in zones:
            if plane.route_progress_m>=end-1e-7:
                continue
            request=world.occupancies.get(plane.crossing_handles.get(cid))
            if request is None or request.status!='active':
                target=min(target,max(plane.route_progress_m,start))
                if target<=plane.route_progress_m+1e-8:
                    plane.wait_reason=f'等待空地交叉区清空：{cid}'
        moved=target-plane.route_progress_m
        plane.route_progress_m=target;plane.distance_m+=moved;plane.current_speed_mps=moved/dt
        if moved<1e-8:plane.wait_s+=dt
        _position(world,plane)
        for start,end,cid in zones:
            request=world.occupancies.get(plane.crossing_handles.get(cid))
            if request and request.status=='active':
                # The forecast may extend while held; actual ownership never
                # expires until the recorded aircraft centre clears the tail.
                request.actual_end=world.now+timedelta(seconds=max(0.,end-target)/plane.speed_mps)
                if target>=end-1e-7:
                    request.status='completed';request.actual_end=world.now
                    record_event(world,'crossing_aircraft_exited',plane.id,occupancy_id=request.id)
        if target>=total-1e-7:
            inbound=plane.state=='taxi_in'
            plane.state='parked' if inbound else 'departed'
            plane.current_speed_mps=0.;plane.next_node_id=None
            plane.node_id=plane.route[-1]
            if inbound:plane.actual_arrival_at=world.now
            else:plane.actual_exit_at=world.now
            world.aircraft_taxi_owner=None
            record_event(world,'aircraft_arrived' if inbound else 'aircraft_departed',plane.id,flight_id=plane.flight_id)


def rows(world):
    return [a.model_dump(mode='json') for a in world.aircraft.values()]
