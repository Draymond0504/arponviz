"""FastAPI 接口：任务、车辆、告警、事件、KPI，并驱动后台模拟循环。"""
from __future__ import annotations

import asyncio
import threading
import copy
import logging
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .. import crossings, facilities, flights, metrics, policies, scheduler, simulator
from ..models import CrossingOccupancy, Flight, Task, TaskType
from ..checkpoint import load_or_create_world
from ..storage import init_db
from .schemas import CloseRoadRequest, CreateTaskRequest, FlightRequest, OccupancyRequest, ResolveAlertRequest

@asynccontextmanager
async def lifespan(app):
    on_startup()
    try:
        yield
    finally:
        on_shutdown()


app = FastAPI(title="ApronOps API", lifespan=lifespan)

_lock = threading.Lock()
_world: simulator.World | None = None
_stop_event = threading.Event()
TICK_INTERVAL_S = 2.0  # 墙钟每 2 秒推进一次
SIM_DT_S = 5.0  # 每次推进代表 5 秒模拟时间


class ConnectionManager:
    """维护当前所有 /ws/state 连接，负责广播；发送失败的连接直接摘除，不影响其余连接。"""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)

    async def broadcast(self, payload: dict) -> None:
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(ws)


_manager = ConnectionManager()
_loop: asyncio.AbstractEventLoop | None = None
_tick_seq = 0
_thread: threading.Thread | None = None
_runtime_error: str | None = None


def _snapshot_payload(world: simulator.World) -> dict:
    global _tick_seq
    _tick_seq += 1
    return {
        "tick": _tick_seq,
        "t": world.now.isoformat(),
        "vehicles": [
            {
                "id": v.id,
                "state": v.state.value,
                "x": v.x,
                "y": v.y,
                "current_task_id": v.current_task_id,
            }
            for v in world.vehicles.values()
        ],
        "unresolved_alerts": sum(1 for a in world.alerts.values() if not a.resolved_at),
    }


@contextmanager
def _mutation():
    """Persist before acknowledging a command; roll back memory on failure."""
    global _world
    with _lock:
        if _runtime_error:
            raise HTTPException(status_code=503, detail="运行已暂停，请检查服务日志并重启")
        previous = copy.deepcopy(_world)
        try:
            yield _world
            _world.revision += 1
            simulator._persist(_world)
        except Exception:
            _world = previous
            raise


def _background_loop() -> None:
    global _world, _runtime_error
    while not _stop_event.wait(TICK_INTERVAL_S):
        with _lock:
            previous = copy.deepcopy(_world)
            try:
                simulator.tick(_world, dt=SIM_DT_S, random_anomaly_prob=0.0)
                payload = _snapshot_payload(_world)
            except Exception:
                _world = previous
                _runtime_error = "运行推进失败，已暂停；请检查服务日志"
                logging.exception("Simulation paused after tick failure")
                return
        if _loop is not None:
            asyncio.run_coroutine_threadsafe(_manager.broadcast(payload), _loop)


def on_startup() -> None:
    global _world, _loop, _thread, _runtime_error
    init_db()
    _world = load_or_create_world()
    _loop = asyncio.get_running_loop()
    _runtime_error = None
    _stop_event.clear()
    _thread = threading.Thread(target=_background_loop, daemon=True)
    _thread.start()


def on_shutdown() -> None:
    global _thread, _loop
    _stop_event.set()
    if _thread is not None:
        _thread.join()
        _thread = None
    _loop = None


@app.websocket("/ws/state")
async def ws_state(websocket: WebSocket) -> None:
    await _manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _manager.disconnect(websocket)


@app.get("/vehicles")
def list_vehicles():
    with _lock:
        return [v.model_dump() for v in _world.vehicles.values()]


@app.get("/tasks")
def list_tasks():
    with _lock:
        return [t.model_dump() for t in _world.tasks.values()]


@app.post("/tasks")
def create_task(req: CreateTaskRequest):
    with _mutation():
        if req.id in _world.tasks:
            raise HTTPException(status_code=409, detail="task id already exists")
        try:
            task_type = TaskType(req.type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown task type: {req.type}")
        if req.origin not in _world.layout.nodes or req.destination not in _world.layout.nodes:
            raise HTTPException(status_code=400, detail="unknown origin/destination node")
        if any(_world.layout.nodes[n].type == 'crossing' for n in (req.origin, req.destination)):
            raise HTTPException(status_code=400, detail="交叉冲突区不能作为作业停靠点")
        task = Task(
            id=req.id,
            type=task_type,
            origin=req.origin,
            destination=req.destination,
            priority=req.priority,
            deadline=_world.now + timedelta(seconds=req.deadline_offset_s),
            created_at=_world.now,
        )
        _world.tasks[task.id] = task
        simulator.record_event(_world, "task_created", "operator", task.id)
        return task.model_dump()


@app.get("/alerts")
def list_alerts():
    with _lock:
        return [a.model_dump() for a in _world.alerts.values()]


@app.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: str, req: ResolveAlertRequest):
    with _mutation():
        if alert_id not in _world.alerts:
            raise HTTPException(status_code=404, detail="alert not found")
        alert = simulator.resolve_alert(_world, alert_id, req.resolution)
        return alert.model_dump()


@app.post("/anomalies/inject")
def inject_anomaly(anomaly_type: str, vehicle_id: str):
    with _mutation():
        if vehicle_id not in _world.vehicles:
            raise HTTPException(status_code=404, detail="vehicle not found")
        try:
            event = simulator.inject_anomaly(_world, anomaly_type, vehicle_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return event.model_dump() if event else {"detail": "no event triggered"}


@app.get("/events")
def list_events(task_id: str | None = None):
    from ..storage import EventORM, get_session

    session = get_session()
    try:
        q = session.query(EventORM)
        if task_id:
            q = q.filter(EventORM.related_task_id == task_id)
        return [
            {
                "id": e.id,
                "timestamp": e.timestamp,
                "source": e.source,
                "event_type": e.event_type,
                "severity": e.severity,
                "related_task_id": e.related_task_id,
                "payload": e.payload,
            }
            for e in q.order_by(EventORM.timestamp.desc()).limit(200).all()
        ]
    finally:
        session.close()


@app.get("/metrics")
def get_metrics():
    with _lock:
        return metrics.compute_metrics(now=_world.now)


@app.get("/tracks")
def list_tracks():
    with _lock:
        return {
            vid: [{"t": t.isoformat(), "x": x, "y": y} for t, x, y in world_track]
            for vid, world_track in _world.tracks.items()
        }


@app.get("/layout")
def get_layout():
    with _lock:
        return {
            "metadata": _world.layout.metadata,
            "reference": _world.layout.reference,
            "nodes": [n.__dict__ for n in _world.layout.nodes.values()],
            "edges": [
                {"from": a, "to": b, "distance_m": d,
                 **_world.layout.edge_attributes.get((a, b), {}),
                 "closed": _world.layout.is_blocked(a, b)}
                for a, neighbors in _world.layout.adjacency.items()
                for b, d, _ in neighbors
            ],
        }


@app.get("/routes")
def get_route(origin: str, destination: str):
    with _lock:
        layout = _world.layout
        if origin not in layout.nodes or destination not in layout.nodes:
            raise HTTPException(status_code=400, detail="unknown origin/destination node")
        try:
            path = layout.shortest_path(origin, destination)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="当前道路封闭，无可通行路线")
        return {"path": path, "distance_m": layout.path_distance(origin, destination),
                "crossings": [n for n in path if layout.nodes[n].type == "crossing"]}


@app.get("/state")
def get_state():
    """One consistent snapshot for map, tables and closure state."""
    with _lock:
        return {
            "revision": _world.revision, "simulation_time": _world.now.isoformat(),
            "runtime_error": _runtime_error,
            "vehicles": [{**v.model_dump(), "wait_reason": _world.route_wait.get(v.id, ""),
                          "recovery_action":facilities.recovery_action(_world,v.id)}
                         for v in _world.vehicles.values()],
            "tasks": [{**t.model_dump(), "wait_reason": scheduler.task_wait_reason(t, _world.tasks, _world.now)}
                      for t in _world.tasks.values()],
            "flights": flights.flight_rows(_world),
            "occupancies": [r.model_dump() for r in _world.occupancies.values()],
            "crossings": crossings.crossing_rows(_world),
            "depots": facilities.rows(_world), "recovery": dict(_world.recovery_counts),
            "policy": _world.policy_name, "dispatch": dict(_world.dispatch_stats),
            "wait_for": copy.deepcopy(_world.wait_for), "deadlock_cycles": copy.deepcopy(_world.deadlock_cycles),
            "alerts": [a.model_dump() for a in _world.alerts.values()],
            "closures": list(_world.closures.values()),
            "tracks": {vid: [{"t": t.isoformat(), "x": x, "y": y} for t, x, y in points]
                       for vid, points in _world.tracks.items()},
        }


@app.get("/roads/closures")
def list_closures():
    with _lock:
        return list(_world.closures.values())


@app.get('/dispatch/policies')
def list_policies():
    with _lock:
        return {'current':_world.policy_name, 'options':policies.POLICIES}


@app.post('/dispatch/policy')
def set_policy(policy: str):
    if policy not in policies.POLICIES:
        raise HTTPException(status_code=400, detail='未知调度策略')
    with _mutation():
        _world.policy_name = policy
        _world.next_dispatch_at = None
        simulator.record_event(_world,'policy_changed','operator',policy=policy)
        return {'policy':policy}


@app.post("/roads/closures")
def close_road(req: CloseRoadRequest):
    with _mutation():
        try:
            return simulator.close_road(_world, req.from_node, req.to_node, req.reason)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@app.post("/roads/closures/{closure_id}/reopen")
def reopen_road(closure_id: str, req: ResolveAlertRequest):
    with _mutation():
        if closure_id not in _world.closures:
            raise HTTPException(status_code=404, detail="unknown road closure")
        return simulator.reopen_road(_world, closure_id, req.resolution)


@app.get("/health")
def health():
    with _lock:
        if _runtime_error:
            raise HTTPException(status_code=503, detail=_runtime_error)
        return {"status": "ok", "revision": _world.revision, "simulation_time": _world.now.isoformat()}


@app.get("/flights")
def list_flights():
    with _lock:
        return flights.flight_rows(_world)


@app.post("/flights")
def save_flight(req: FlightRequest):
    with _mutation():
        try:
            flight = flights.upsert_flight(_world, Flight(**req.model_dump(exclude={'expected_revision'})), req.expected_revision)
            return flight.model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))


@app.get("/crossings/occupancies")
def list_occupancies():
    with _lock:
        return [r.model_dump() for r in _world.occupancies.values()]


@app.post("/crossings/occupancies")
def add_occupancy(req: OccupancyRequest):
    with _mutation():
        try:
            result = crossings.register_occupancy(_world, CrossingOccupancy(**req.model_dump()))
            crossings.update_crossings(_world)
            return result.model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))


@app.post("/scenarios/turnaround-demo")
def load_turnaround_demo():
    with _mutation():
        return flights.load_demo(_world).model_dump()
