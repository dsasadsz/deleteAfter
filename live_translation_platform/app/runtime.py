from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class TaskStatus:
    name: str
    status: str = "running"
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    last_error: str | None = None


class BackgroundTaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskStatus] = {}

    def start(self, name: str) -> None:
        self._tasks[name] = TaskStatus(name=name)

    def complete(self, name: str) -> None:
        status = self._tasks.get(name) or TaskStatus(name=name)
        status.status = "completed"
        status.completed_at = datetime.utcnow()
        self._tasks[name] = status

    def error(self, name: str, error: str) -> None:
        status = self._tasks.get(name) or TaskStatus(name=name)
        status.status = "error"
        status.completed_at = datetime.utcnow()
        status.last_error = error
        self._tasks[name] = status

    def list(self) -> list[dict]:
        return [
            {
                "name": item.name,
                "status": item.status,
                "started_at": item.started_at.isoformat(),
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                "last_error": item.last_error,
            }
            for item in self._tasks.values()
        ]


async def shutdown_runtime(app: Any) -> None:
    timeout = getattr(getattr(app.state, "settings", None), "worker_shutdown_timeout_seconds", 30)
    await _stop_session_manager(getattr(app.state, "session_manager", None), timeout)
    await _stop_rtms_manager(getattr(app.state, "rtms_manager", None), timeout)
    await _close_redis_client(getattr(app.state, "redis", None))


async def _stop_session_manager(session_manager: Any, timeout: int) -> None:
    if session_manager is None:
        return
    lesson_ids = list(getattr(session_manager, "sessions", {}).keys())
    for lesson_id in lesson_ids:
        await asyncio.wait_for(session_manager.stop(lesson_id), timeout=timeout)


async def _stop_rtms_manager(rtms_manager: Any, timeout: int) -> None:
    if rtms_manager is None:
        return
    lesson_ids = list(getattr(rtms_manager, "clients", {}).keys())
    for lesson_id in lesson_ids:
        await asyncio.wait_for(rtms_manager.stop_lesson(lesson_id), timeout=timeout)


async def _close_redis_client(client: Any) -> None:
    if client is None:
        return
    from app.infra.redis import close_redis_client

    await close_redis_client(client)
