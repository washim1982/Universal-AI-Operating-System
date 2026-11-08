"""Where Human OS keeps work assigned to people (FR-HUM-08).

The first answer wins, as with approvals: completing a task and publishing what happened are one
step, so two people who finish it at once can't both be told they did, and a run can't hear twice.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from pydantic import JsonValue

from aios.core.envelope import Event
from aios.core.human import HumanTask, TaskStatus
from aios.fabric.port import EventBus

INBOX_LIMIT = 200


class TaskStore(ABC):
    @abstractmethod
    async def add(self, task: HumanTask, *, events: Sequence[Event]) -> bool:
        """Record a task and publish ``events``, as one step. ``False`` means it was there
        already, and nothing was published."""

    @abstractmethod
    async def get(self, tenant: str, task_id: str) -> HumanTask | None: ...

    @abstractmethod
    async def pending(self, tenant: str, *, limit: int = INBOX_LIMIT) -> list[HumanTask]:
        """Open tasks of a tenant, oldest first."""

    @abstractmethod
    async def in_run(self, tenant: str, run_id: str) -> list[HumanTask]:
        """Open tasks of one run, to close when it ends."""

    @abstractmethod
    async def close(
        self,
        task: HumanTask,
        *,
        status: TaskStatus,
        by: str,
        output: JsonValue,
        comment: str | None,
        at: datetime,
        events: Sequence[Event],
    ) -> HumanTask | None:
        """Close an open task and publish ``events``, as one step. ``None`` means it was closed
        already, and nothing was published."""


class InMemoryTaskStore(TaskStore):
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._tasks: dict[tuple[str, str], HumanTask] = {}

    async def add(self, task: HumanTask, *, events: Sequence[Event]) -> bool:
        key = (task.tenant, task.id)
        if key in self._tasks:
            return False
        await self.bus.publish(*events)  # first: if this fails, nothing is recorded
        self._tasks[key] = task
        return True

    async def get(self, tenant: str, task_id: str) -> HumanTask | None:
        return self._tasks.get((tenant, task_id))

    async def pending(self, tenant: str, *, limit: int = INBOX_LIMIT) -> list[HumanTask]:
        keys = sorted(k for k in self._tasks if k[0] == tenant)
        return [t for t in (self._tasks[k] for k in keys) if t.status == "pending"][:limit]

    async def in_run(self, tenant: str, run_id: str) -> list[HumanTask]:
        return [t for t in await self.pending(tenant, limit=10_000) if t.run == run_id]

    async def close(
        self,
        task: HumanTask,
        *,
        status: TaskStatus,
        by: str,
        output: JsonValue,
        comment: str | None,
        at: datetime,
        events: Sequence[Event],
    ) -> HumanTask | None:
        key = (task.tenant, task.id)
        current = self._tasks.get(key)
        if current is None or current.status != "pending":
            return None
        closed = replace(
            current, status=status, by=by, output=output, comment=comment, completed_at=at
        )
        await self.bus.publish(*events)
        self._tasks[key] = closed
        return closed
