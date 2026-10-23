"""FastAPI 接口：任务、车辆、告警、事件、KPI，并驱动后台模拟循环。"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import timedelta

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .. import metrics, simulator
from ..models import Task, TaskStatus, TaskType
from ..state_machine import transition_task
from ..storage import init_db
from .schemas import CreateTaskRequest, ResolveAlertRequest

app = FastAPI(title="ApronOps API")

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


def _background_loop() -> None:
    while not _stop_event.is_set():
        time.sleep(TICK_INTERVAL_S)
        with _lock:
            if _world is not None:
                simulator.tick(_world, dt=SIM_DT_S, random_anomaly_prob=0.01)
                payload = _snapshot_payload(_world)
        if _loop is not None:
            asyncio.run_coroutine_threadsafe(_manager.broadcast(payload), _loop)


@app.on_event("startup")
def on_startup() -> None:
    global _world, _loop
    init_db()
    _world = simulator.load_world()
    _loop = asyncio.get_running_loop()
    thread = threading.Thread(target=_background_loop, daemon=True)
    thread.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    _stop_event.set()


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
    with _lock:
        if req.id in _world.tasks:
            raise HTTPException(status_code=409, detail="task id already exists")
        try:
            task_type = TaskType(req.type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown task type: {req.type}")
        if req.origin not in _world.layout.nodes or req.destination not in _world.layout.nodes:
            raise HTTPException(status_code=400, detail="unknown origin/destination node")
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
        return task.model_dump()


@app.get("/alerts")
def list_alerts():
    with _lock:
        return [a.model_dump() for a in _world.alerts.values()]


@app.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: str, req: ResolveAlertRequest):
    with _lock:
        if alert_id not in _world.alerts:
            raise HTTPException(status_code=404, detail="alert not found")
        alert = simulator.resolve_alert(_world, alert_id, req.resolution)
        return alert.model_dump()


@app.post("/anomalies/inject")
def inject_anomaly(anomaly_type: str, vehicle_id: str):
    with _lock:
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
    return metrics.compute_metrics()


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
                 **_world.layout.edge_attributes.get((a, b), {})}
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
            raise HTTPException(status_code=400, detail=str(exc))
        return {"path": path, "distance_m": layout.path_distance(origin, destination),
                "crossings": [n for n in path if layout.nodes[n].type == "crossing"]}
