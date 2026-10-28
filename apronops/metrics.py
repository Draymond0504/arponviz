"""KPI 计算：从内存 World / 数据库读取，输出蓝图 §8 指标。"""
from __future__ import annotations

from datetime import datetime

from .storage import AlertORM, TaskORM, get_session


def compute_metrics(now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    session = get_session()
    try:
        tasks = session.query(TaskORM).all()
        alerts = session.query(AlertORM).all()

        total = len(tasks)
        completed = [t for t in tasks if t.status == "COMPLETED"]
        failed = [t for t in tasks if t.status == "FAILED"]

        on_time = [
            t for t in completed
            if t.deadline is None or (t.completed_at and t.completed_at <= t.deadline)
        ]

        durations = [
            (t.completed_at - t.started_at).total_seconds()
            for t in completed if t.started_at and t.completed_at
        ]
        response_times = [
            (t.started_at - t.created_at).total_seconds()
            for t in completed if t.started_at
        ]

        due = [t for t in tasks if t.deadline is not None and t.deadline <= now]
        due_on_time = [t for t in due if t.status == "COMPLETED" and t.completed_at and t.completed_at <= t.deadline]
        overdue_unfinished = [t for t in due if t.status != "COMPLETED"]
        resolved_alerts = [a for a in alerts if a.resolved_at is not None]

        return {
            "task_total": total,
            "task_completed": len(completed),
            "task_failed": len(failed),
            "completion_rate": round(len(completed) / total, 3) if total else 0.0,
            "on_time_rate": round(len(due_on_time) / len(due), 3) if due else 0.0,
            "due_task_total": len(due),
            "overdue_unfinished": len(overdue_unfinished),
            "completed_on_time_rate": round(len(on_time) / len(completed), 3) if completed else 0.0,
            "avg_response_time_s": round(sum(response_times) / len(response_times), 1) if response_times else 0.0,
            "avg_completion_time_s": round(sum(durations) / len(durations), 1) if durations else 0.0,
            "alert_total": len(alerts),
            "alert_resolved": len(resolved_alerts),
            "alert_resolution_rate": round(len(resolved_alerts) / len(alerts), 3) if alerts else 0.0,
        }
    finally:
        session.close()
