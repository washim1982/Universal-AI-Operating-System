"""Approvals in the fabric's PostgreSQL database.

Closing an approval and publishing what happened are one transaction, through the fabric's outbox
(FR-EVT-07), so an approval can't be recorded as decided while its run never hears of it. Reads for
a tenant go through a tenant connection; writes use the connecting role, because a tenant
connection can only read, and name the tenant themselves.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import JsonValue

from aios.core.catalog import HumanTaskAssigned
from aios.core.envelope import Event
from aios.core.human import Approval, ApprovalStatus, HumanTask, TaskStatus, Vote
from aios.fabric.postgres import TENANT_ROLE, PostgresEventBus, tenant_only
from aios.human.store import INBOX_LIMIT, OVERDUE_BATCH, ApprovalStore
from aios.human.tasks import INBOX_LIMIT as TASK_INBOX_LIMIT
from aios.human.tasks import TaskStore

_DDL = """
CREATE TABLE IF NOT EXISTS {s}.approvals (
    tenantid     text NOT NULL,
    id           text NOT NULL,
    runid        text,
    status       text NOT NULL,
    approved     boolean NOT NULL DEFAULT false,
    expires_at   timestamptz NOT NULL,
    request      jsonb NOT NULL,
    decided_by   text,
    comment      text,
    decided_at   timestamptz,
    PRIMARY KEY (tenantid, id)
);
ALTER TABLE {s}.approvals ADD COLUMN IF NOT EXISTS progress jsonb NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS approvals_inbox ON {s}.approvals (tenantid, status, id);
CREATE INDEX IF NOT EXISTS approvals_due ON {s}.approvals (expires_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS approvals_run ON {s}.approvals (tenantid, runid)
    WHERE status = 'pending';
"""


def _approval(row: Any) -> Approval:
    return Approval.of(
        Event.model_validate(row["request"]),
        status=row["status"],
        approved=row["approved"],
        by=row["decided_by"],
        comment=row["comment"],
        decided_at=row["decided_at"],
        progress=row["progress"],
    )


class PostgresApprovalStore(ApprovalStore):
    def __init__(self, bus: PostgresEventBus) -> None:
        self.bus = bus
        self._ready = False
        self._lock = asyncio.Lock()

    async def _setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            schema = self.bus.schema
            statements = (
                self.bus.sql(_DDL),
                tenant_only(schema, "approvals"),
                f"GRANT SELECT ON {schema}.approvals TO {TENANT_ROLE};",
            )
            await self.bus.set_up("approvals", "\n".join(statements))
            self._ready = True

    async def add(self, approval: Approval) -> None:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            await connection.execute(
                self.bus.sql(
                    "INSERT INTO {s}.approvals (tenantid, id, runid, status, expires_at, request) "
                    "VALUES ($1, $2, $3, 'pending', $4, $5) ON CONFLICT DO NOTHING"
                ),
                approval.tenant,
                approval.id,
                approval.run,
                approval.expires_at,
                approval.request.model_dump(mode="json"),
            )

    async def get(self, tenant: str, approval_id: str) -> Approval | None:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            row = await connection.fetchrow(
                self.bus.sql("SELECT * FROM {s}.approvals WHERE id = $1"), approval_id
            )
        return None if row is None else _approval(row)

    async def find(
        self, tenant: str, status: ApprovalStatus, *, limit: int = INBOX_LIMIT
    ) -> list[Approval]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql("SELECT * FROM {s}.approvals WHERE status = $1 ORDER BY id LIMIT $2"),
                status,
                limit,
            )
        return [_approval(row) for row in rows]

    async def in_run(self, tenant: str, run_id: str) -> list[Approval]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql(
                    "SELECT * FROM {s}.approvals WHERE runid = $1 AND status = 'pending' "
                    "ORDER BY id"
                ),
                run_id,
            )
        return [_approval(row) for row in rows]

    async def overdue(self, now: datetime, *, limit: int = OVERDUE_BATCH) -> list[Approval]:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            rows = await connection.fetch(
                self.bus.sql(
                    "SELECT * FROM {s}.approvals WHERE status = 'pending' AND expires_at <= $1 "
                    "ORDER BY expires_at LIMIT $2"
                ),
                now,
                limit,
            )
        return [_approval(row) for row in rows]

    async def vote(self, approval: Approval, vote: Vote, *, event: Event) -> Approval | None:
        await self._setup()
        added = [{"by": vote.by, "at": vote.at.isoformat(), "comment": vote.comment}]
        async with self.bus.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                self.bus.sql(
                    "UPDATE {s}.approvals SET progress = jsonb_set(progress, '{votes}', "
                    "coalesce(progress->'votes', '[]'::jsonb) || $3::jsonb) "
                    "WHERE tenantid = $1 AND id = $2 AND status = 'pending' "
                    "AND NOT coalesce(progress->'votes', '[]'::jsonb) @> $4::jsonb RETURNING *"
                ),
                approval.tenant,
                approval.id,
                added,
                [{"by": vote.by}],
            )
            if row is None:
                return None
            await self.bus.publish_in(connection, event)
        return _approval(row)

    async def advance(
        self, approval: Approval, changed: Approval, *, event: Event
    ) -> Approval | None:
        await self._setup()
        async with self.bus.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                self.bus.sql(
                    "UPDATE {s}.approvals SET progress = progress || $3 "
                    "WHERE tenantid = $1 AND id = $2 AND status = 'pending' "
                    "AND coalesce((progress->>'level')::int, 0) = $4 "
                    "AND coalesce((progress->>'reminders')::int, 0) = $5 RETURNING *"
                ),
                approval.tenant,
                approval.id,
                {k: v for k, v in changed.progress().items() if k != "votes"},
                approval.level,
                approval.reminders,
            )
            if row is None:
                return None
            await self.bus.publish_in(connection, event)
        return _approval(row)

    async def waiting(self, *, limit: int = OVERDUE_BATCH) -> list[Approval]:
        await self._setup()
        async with self.bus.pool.acquire() as connection:
            rows = await connection.fetch(
                self.bus.sql(
                    "SELECT * FROM {s}.approvals WHERE status = 'pending' "
                    "AND ((request #>> '{data,remind_every}') IS NOT NULL "
                    "OR jsonb_array_length(coalesce(request #> '{data,escalation}', "
                    "'[]'::jsonb)) > 0) ORDER BY expires_at LIMIT $1"
                ),
                limit,
            )
        return [_approval(row) for row in rows]

    async def settle(
        self,
        approval: Approval,
        *,
        status: ApprovalStatus,
        approved: bool,
        by: str,
        comment: str | None,
        at: datetime,
        event: Event,
        edits: dict[str, JsonValue] | None = None,
    ) -> Approval | None:
        await self._setup()
        async with self.bus.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                self.bus.sql(
                    "UPDATE {s}.approvals SET status = $3, approved = $4, decided_by = $5, "
                    "comment = $6, decided_at = $7, progress = progress || $8 "
                    "WHERE tenantid = $1 AND id = $2 AND status = 'pending' RETURNING *"
                ),
                approval.tenant,
                approval.id,
                status,
                approved,
                by,
                comment,
                at,
                {"edits": edits} if edits is not None else {},
            )
            if row is None:
                return None
            await self.bus.publish_in(connection, event)
        return _approval(row)


_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS {s}.human_tasks (
    tenantid     text NOT NULL,
    id           text NOT NULL,
    runid        text,
    status       text NOT NULL,
    request      jsonb NOT NULL,
    details      jsonb NOT NULL,
    done_by      text,
    output       jsonb,
    comment      text,
    completed_at timestamptz,
    PRIMARY KEY (tenantid, id)
);
CREATE INDEX IF NOT EXISTS human_tasks_open ON {s}.human_tasks (tenantid, id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS human_tasks_run ON {s}.human_tasks (tenantid, runid)
    WHERE status = 'pending';
"""


def _task(row: Any) -> HumanTask:
    return HumanTask(
        Event.model_validate(row["request"]),
        HumanTaskAssigned.model_validate(row["details"]),
        status=row["status"],
        by=row["done_by"],
        output=row["output"],
        comment=row["comment"],
        completed_at=row["completed_at"],
    )


class PostgresTaskStore(TaskStore):
    """Work for people, closed with its events in one transaction, as approvals are."""

    def __init__(self, bus: PostgresEventBus) -> None:
        self.bus = bus
        self._ready = False
        self._lock = asyncio.Lock()

    async def _setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            schema = self.bus.schema
            statements = (
                self.bus.sql(_TASKS_DDL),
                tenant_only(schema, "human_tasks"),
                f"GRANT SELECT ON {schema}.human_tasks TO {TENANT_ROLE};",
            )
            await self.bus.set_up("human_tasks", "\n".join(statements))
            self._ready = True

    async def add(self, task: HumanTask, *, events: Sequence[Event]) -> bool:
        await self._setup()
        async with self.bus.pool.acquire() as connection, connection.transaction():
            added = await connection.fetchval(
                self.bus.sql(
                    "INSERT INTO {s}.human_tasks (tenantid, id, runid, status, request, details) "
                    "VALUES ($1, $2, $3, 'pending', $4, $5) ON CONFLICT DO NOTHING RETURNING id"
                ),
                task.tenant,
                task.id,
                task.run,
                task.request.model_dump(mode="json"),
                task.details.model_dump(mode="json"),
            )
            if added is None:
                return False
            await self.bus.publish_in(connection, *events)
        return True

    async def get(self, tenant: str, task_id: str) -> HumanTask | None:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            row = await connection.fetchrow(
                self.bus.sql("SELECT * FROM {s}.human_tasks WHERE id = $1"), task_id
            )
        return None if row is None else _task(row)

    async def pending(self, tenant: str, *, limit: int = TASK_INBOX_LIMIT) -> list[HumanTask]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql(
                    "SELECT * FROM {s}.human_tasks WHERE status = 'pending' ORDER BY id LIMIT $1"
                ),
                limit,
            )
        return [_task(row) for row in rows]

    async def in_run(self, tenant: str, run_id: str) -> list[HumanTask]:
        await self._setup()
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(
                self.bus.sql(
                    "SELECT * FROM {s}.human_tasks WHERE runid = $1 AND status = 'pending' "
                    "ORDER BY id"
                ),
                run_id,
            )
        return [_task(row) for row in rows]

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
        await self._setup()
        async with self.bus.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                self.bus.sql(
                    "UPDATE {s}.human_tasks SET status = $3, done_by = $4, output = $5, "
                    "comment = $6, completed_at = $7 "
                    "WHERE tenantid = $1 AND id = $2 AND status = 'pending' RETURNING *"
                ),
                task.tenant,
                task.id,
                status,
                by,
                output,
                comment,
                at,
            )
            if row is None:
                return None
            await self.bus.publish_in(connection, *events)
        return _task(row)
