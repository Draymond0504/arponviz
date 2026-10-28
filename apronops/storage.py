"""SQLite 持久化层（SQLAlchemy），落库 Vehicle / Task / Event / Alert。"""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class VehicleORM(Base):
    __tablename__ = "vehicles"
    id = Column(String, primary_key=True)
    type = Column(String, nullable=False)
    node_id = Column(String, nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    speed_mps = Column(Float, nullable=False)
    current_speed_mps = Column(Float, default=0.0)
    state = Column(String, nullable=False)
    battery = Column(Float, nullable=False)
    current_task_id = Column(String, nullable=True)
    target_node_id = Column(String, nullable=True)
    next_node_id = Column(String, nullable=True)
    edge_progress_m = Column(Float, default=0.0)
    task_phase = Column(String, nullable=True)
    service_remaining_s = Column(Float, default=0.0)
    last_heartbeat = Column(DateTime, nullable=False)
    position_quality_streak_low = Column(Integer, default=0)


class TaskORM(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True)
    type = Column(String, nullable=False)
    origin = Column(String, nullable=False)
    destination = Column(String, nullable=False)
    priority = Column(Integer, default=3)
    deadline = Column(DateTime, nullable=True)
    status = Column(String, nullable=False)
    assigned_vehicle_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class EventORM(Base):
    __tablename__ = "events"
    id = Column(String, primary_key=True)
    timestamp = Column(DateTime, nullable=False)
    source = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    related_task_id = Column(String, nullable=True)
    payload = Column(JSON, default=dict)


class AlertORM(Base):
    __tablename__ = "alerts"
    id = Column(String, primary_key=True)
    event_id = Column(String, nullable=False)
    anomaly_type = Column(String, nullable=False)
    impact_scope = Column(JSON, default=list)
    suggested_action = Column(String, nullable=False)
    resolution = Column(String, nullable=True)
    resolved_at = Column(DateTime, nullable=True)


class RuntimeSnapshotORM(Base):
    """One atomic checkpoint, committed with projections and operation events."""
    __tablename__ = "runtime_snapshot"
    id = Column(Integer, primary_key=True)
    payload = Column(JSON, nullable=False)


_engine = None
_SessionLocal: sessionmaker | None = None


def init_db(db_path: str | None = None) -> None:
    global _engine, _SessionLocal
    db_path = db_path or os.environ.get("APRONOPS_DB_PATH", "apronops-operations-v1.db")
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    if _SessionLocal is None:
        init_db()
    return _SessionLocal()
