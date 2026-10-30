"""PostgreSQL ``EventBus`` adapter (WP 1.1; ADR-004; requirements §8.1).

* **Outbox = log.** ``publish`` inserts rows in one transaction; ``publish_in`` does it inside
  the caller's own transaction, so state changes and their events commit together (FR-EVT-07).
* **Read-horizon sequencer.** One leader (``pg_try_advisory_lock``) gives committed rows gapless
  positions in ``(tx, seq)`` order, only for transactions older than the snapshot ``xmin``, so
  commits out of order can't make a reader skip an event. Validated by ``spikes/pg_sequencer.py``.
* **Fan-out** to consumer groups happens in the sequencer's transaction, matched with the same
  Python matcher as everywhere else; creating a group locks the sequencer row, so a group never
  misses or doubles an event.
* **Claims** use ``SELECT … FOR UPDATE SKIP LOCKED`` with leases, skipping rows whose run has an
  earlier unsettled row in the group (per-run order). Ack deletes the row; past
  ``max_deliveries`` the row moves to ``dead_letters`` and ``system.dlq.added`` is appended in
  the same transaction.
* **Read your writes.** ``replay`` first waits until every committed event is sequenced.

Wake-ups are payload-free NOTIFYs with a polling fallback. Known limits for WP 4.x: the
``events`` table is not partitioned yet, and an ephemeral group left by a crashed process is
not garbage-collected.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import asyncpg

from aios.core.catalog import DeadLetterAdded
from aios.core.envelope import Event
from aios.core.errors import BusClosedError, NotFoundError, SubscriptionConflictError
from aios.core.ids import new_ulid
from aios.core.topics import matches, validate_name, validate_pattern
from aios.fabric.port import (
    DEFAULT_MAX_HOPS,
    DeadLetter,
    Delivery,
    EventBus,
    RetryPolicy,
    Scope,
    StartFrom,
    StoredEvent,
    Subscription,
)
from aios.fabric.subjects import physical_pattern, physical_subject

logger = logging.getLogger("aios.fabric.postgres")

DLQ_EVENT_TYPE = "system.dlq.added"
POLL_SECONDS = 0.05
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_DDL = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.events (
    tx       xid8        NOT NULL DEFAULT pg_current_xact_id(),
    seq      bigint      GENERATED ALWAYS AS IDENTITY,
    position bigint      UNIQUE,
    id       text        NOT NULL,
    type     text        NOT NULL,
    subject  text        NOT NULL,
    tenantid text        NOT NULL,
    appid    text        NOT NULL,
    runid    text,
    time     timestamptz NOT NULL,
    body     jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS events_unsequenced ON {s}.events (tx, seq) WHERE position IS NULL;
CREATE INDEX IF NOT EXISTS events_scope ON {s}.events (tenantid, appid, position);
CREATE INDEX IF NOT EXISTS events_run ON {s}.events (runid, position) WHERE runid IS NOT NULL;
CREATE TABLE IF NOT EXISTS {s}.sequencer (
    id int PRIMARY KEY CHECK (id = 1),
    position bigint NOT NULL
);
INSERT INTO {s}.sequencer VALUES (1, 0) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS {s}.groups (
    key       text PRIMARY KEY,
    tenant    text NOT NULL,
    app       text NOT NULL,
    name      text NOT NULL,
    pattern   text NOT NULL,
    physical  text NOT NULL,
    durable   boolean NOT NULL,
    ack_wait  double precision NOT NULL,
    retry     jsonb NOT NULL,
    members   integer NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {s}.deliveries (
    group_key      text NOT NULL REFERENCES {s}.groups (key) ON DELETE CASCADE,
    position       bigint NOT NULL,
    run_key        text,
    attempt        integer NOT NULL DEFAULT 0,
    not_before     timestamptz NOT NULL DEFAULT '-infinity',
    lease_token    text,
    lease_owner    text,
    lease_deadline timestamptz,
    PRIMARY KEY (group_key, position)
);
CREATE INDEX IF NOT EXISTS deliveries_run ON {s}.deliveries (group_key, run_key, position);
CREATE TABLE IF NOT EXISTS {s}.dead_letters (
    id        text PRIMARY KEY,
    group_key text NOT NULL,
    group_name text NOT NULL,
    position  bigint NOT NULL,
    attempts  integer NOT NULL,
    reason    text NOT NULL,
    dead_at   timestamptz NOT NULL,
    tenantid  text NOT NULL,
    appid     text NOT NULL
);
CREATE OR REPLACE FUNCTION {s}.wake_sequencer() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('{s}_events', '');
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS events_wake ON {s}.events;
CREATE TRIGGER events_wake AFTER INSERT ON {s}.events
    FOR EACH STATEMENT EXECUTE FUNCTION {s}.wake_sequencer();
"""

_SEQUENCE = """
WITH ready AS (
    SELECT tx, seq FROM {s}.events
    WHERE position IS NULL AND tx < pg_snapshot_xmin(pg_current_snapshot())
    ORDER BY tx, seq
    LIMIT $1
    FOR UPDATE
), numbered AS (
    SELECT tx, seq, row_number() OVER (ORDER BY tx, seq) AS n FROM ready
), advanced AS (
    UPDATE {s}.sequencer SET position = position + (SELECT count(*) FROM numbered)
    WHERE id = 1
    RETURNING position - (SELECT count(*) FROM numbered) AS base
)
UPDATE {s}.events AS e SET position = advanced.base + numbered.n
FROM numbered, advanced
WHERE e.tx = numbered.tx AND e.seq = numbered.seq
RETURNING e.position, e.subject, e.runid
"""

_CLAIM = """
WITH candidate AS (
    SELECT d.position FROM {s}.deliveries d
    WHERE d.group_key = $1
      AND (d.lease_deadline IS NULL OR d.lease_deadline < clock_timestamp())
      AND d.not_before <= clock_timestamp()
      AND NOT EXISTS (
          SELECT 1 FROM {s}.deliveries e
          WHERE e.group_key = d.group_key AND e.run_key = d.run_key AND e.position < d.position
      )
    ORDER BY d.position
    LIMIT 1
    FOR UPDATE SKIP LOCKED
), claimed AS (
    UPDATE {s}.deliveries AS d
    SET attempt = d.attempt + 1, lease_token = $2, lease_owner = $3,
        lease_deadline = clock_timestamp() + make_interval(secs => $4)
    FROM candidate
    WHERE d.group_key = $1 AND d.position = candidate.position
    RETURNING d.position, d.attempt
)
SELECT claimed.position, claimed.attempt, e.body
FROM claimed JOIN {s}.events e ON e.position = claimed.position
"""


async def _init_connection(connection: asyncpg.Connection) -> None:
    await connection.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


def _event(body: Any) -> Event:
    return Event.model_validate(body)


class _Group:
    def __init__(self, row: Any) -> None:
        self.key: str = row["key"]
        self.name: str = row["name"]
        self.tenant: str = row["tenant"]
        self.app: str = row["app"]
        self.pattern: str = row["pattern"]
        self.physical: str = row["physical"]
        self.durable: bool = row["durable"]
        self.ack_wait: float = row["ack_wait"]
        retry = row["retry"]
        self.retry = RetryPolicy(**retry)

    @property
    def scope(self) -> Scope:
        return Scope(self.tenant, self.app)


class _PostgresDelivery(Delivery):
    def __init__(
        self, bus: PostgresEventBus, group: _Group, stored: StoredEvent, attempt: int, token: str
    ) -> None:
        super().__init__(stored, attempt=attempt, group=group.name)
        self._bus = bus
        self._group = group
        self._token = token

    async def ack(self) -> None:
        await self._bus.settle(self._group, self.position, self._token)

    async def nack(self, *, delay: float | None = None, reason: str = "nacked") -> None:
        await self._bus.retry(self._group, self.position, self._token, delay, reason)

    async def extend(self) -> None:
        await self._bus.extend(self._group, self.position, self._token)


class _PostgresSubscription(Subscription):
    def __init__(self, bus: PostgresEventBus, group: _Group) -> None:
        super().__init__(group.scope, group.pattern, group.name)
        self._bus = bus
        self._group = group
        self._closed = False
        self.member = new_ulid()

    @property
    def closed(self) -> bool:
        return self._closed or self._bus.closed

    async def next(self, timeout: float | None = None) -> Delivery | None:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while not self.closed:
            signal = self._bus.signal
            try:
                delivery = await self._bus.claim(self._group, self.member)
            except (asyncpg.InterfaceError, asyncpg.ConnectionDoesNotExistError):
                if self.closed:
                    return None
                raise
            if delivery is not None:
                return delivery
            wait = POLL_SECONDS * 2
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                wait = min(wait, remaining)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    await signal.wait()
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._bus.closed:
            await self._bus.leave(self._group, self.member)


class PostgresEventBus(EventBus):
    """The durable fabric for the MVP. ``schema`` isolates installations (and tests)."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "aios",
        max_hops: int = DEFAULT_MAX_HOPS,
        validate_payloads: bool = False,
        pool_size: int = 10,
        drop_on_close: bool = False,
    ) -> None:
        super().__init__(max_hops=max_hops, validate_payloads=validate_payloads)
        if not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError(f"invalid schema name {schema!r}")
        self.dsn = dsn
        self.schema = schema
        self.pool_size = pool_size
        self.drop_on_close = drop_on_close
        self.signal = asyncio.Event()
        self._pool: asyncpg.Pool | None = None
        self._listener: asyncpg.Connection | None = None
        self._sequencer: asyncio.Task[None] | None = None
        self._sequencer_wake = asyncio.Event()
        self._members: list[_PostgresSubscription] = []
        self._closed = False

    def _sql(self, template: str) -> str:
        return template.replace("{s}", self.schema)

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise BusClosedError("call connect() first")
        return self._pool

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        """Create the schema if needed, open the pool and start listening and sequencing."""
        self._pool = await asyncpg.create_pool(
            self.dsn, min_size=2, max_size=self.pool_size, init=_init_connection
        )
        async with self.pool.acquire() as connection:
            await connection.execute(self._sql(_DDL))
        listener = await asyncpg.connect(self.dsn)
        await listener.add_listener(f"{self.schema}_events", self._on_events)
        await listener.add_listener(f"{self.schema}_deliveries", self._on_deliveries)
        self._listener = listener
        self._sequencer = asyncio.create_task(self._run_sequencer())

    async def __aenter__(self) -> PostgresEventBus:
        if self._pool is None:
            await self.connect()
        return self

    def _on_events(self, *_: object) -> None:
        self._sequencer_wake.set()

    def _on_deliveries(self, *_: object) -> None:
        self.notify()

    def notify(self) -> None:
        self.signal.set()
        self.signal = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        if self._closed:
            return
        for member in list(self._members):
            with contextlib.suppress(Exception):
                await member.close()
        self._closed = True
        self.notify()
        if self._sequencer is not None:
            self._sequencer.cancel()
            await asyncio.gather(self._sequencer, return_exceptions=True)
        if self._listener is not None:
            await self._listener.close()
        if self._pool is not None:
            if self.drop_on_close:
                async with self._pool.acquire() as connection:
                    await connection.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
            await self._pool.close()

    # ------------------------------------------------------------------ publishing

    @staticmethod
    def _row(event: Event, subject: str) -> tuple[Any, ...]:
        return (
            event.id,
            event.type,
            subject,
            event.tenantid,
            event.appid,
            event.runid,
            event.time,
            event.model_dump(mode="json"),
        )

    _INSERT = (
        "INSERT INTO {s}.events (id, type, subject, tenantid, appid, runid, time, body) "
        "SELECT * FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], "
        "$6::text[], $7::timestamptz[], $8::jsonb[])"
    )

    async def _append(self, batch: Sequence[tuple[Event, str]]) -> None:
        async with self.pool.acquire() as connection:
            await self._insert(connection, batch)

    async def _insert(self, connection: Any, batch: Sequence[tuple[Event, str]]) -> None:
        """One statement for the whole batch: atomic even outside a transaction."""
        if batch:
            rows = [self._row(event, subject) for event, subject in batch]
            columns = [list(column) for column in zip(*rows, strict=True)]
            await connection.execute(self._sql(self._INSERT), *columns)

    async def publish_in(self, connection: Any, *events: Event) -> None:
        """Publish inside the caller's transaction: the transactional outbox (FR-EVT-07)."""
        if self.closed:
            raise BusClosedError("the event bus is closed")
        batch = [(self._check(event), physical_subject(event)) for event in events]
        await self._insert(connection, batch)

    # ------------------------------------------------------------------ sequencer

    async def _run_sequencer(self) -> None:
        """Assign positions while holding the leader lock; standby otherwise."""
        key = int.from_bytes(self.schema.encode()[:8].ljust(8, b"\0"), "big") & 0x7FFFFFFFFFFFFFFF
        while not self._closed:
            try:
                connection = await asyncpg.connect(self.dsn)
            except (OSError, asyncpg.PostgresError):
                await asyncio.sleep(1)
                continue
            try:
                await _init_connection(connection)
                while True:  # another process may be the leader: take over if it dies
                    if await connection.fetchval("SELECT pg_try_advisory_lock($1)", key):
                        break
                    await asyncio.sleep(1)
                while not self._closed:
                    self._sequencer_wake.clear()
                    if await self._sequence(connection):
                        continue
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(POLL_SECONDS):
                            await self._sequencer_wake.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sequencer failed; restarting")
                await asyncio.sleep(0.5)
            finally:
                with contextlib.suppress(Exception):
                    await connection.close()

    async def _sequence(self, connection: Any, batch: int = 5000) -> int:
        async with connection.transaction():
            rows = await connection.fetch(self._sql(_SEQUENCE), batch)
            if not rows:
                return 0
            # A new statement, after the sequencer row is locked: it sees every group whose
            # creation committed first, and groups created later start after these positions.
            groups = await connection.fetch(self._sql("SELECT key, physical FROM {s}.groups"))
            keys: list[str] = []
            positions: list[int] = []
            runs: list[str | None] = []
            for row in rows:
                for group in groups:
                    if matches(group["physical"], row["subject"]):
                        keys.append(group["key"])
                        positions.append(row["position"])
                        runs.append(row["runid"])
            if keys:
                await connection.execute(
                    self._sql(
                        "INSERT INTO {s}.deliveries (group_key, position, run_key) "
                        "SELECT * FROM unnest($1::text[], $2::bigint[], $3::text[])"
                    ),
                    keys,
                    positions,
                    runs,
                )
            await connection.execute(f"SELECT pg_notify('{self.schema}_deliveries', '')")
        self.notify()
        return len(rows)

    async def _barrier(self, timeout: float = 10.0) -> None:
        """Wait until every committed event has a position (read your own writes)."""
        query = self._sql(
            "SELECT EXISTS (SELECT 1 FROM {s}.events WHERE position IS NULL "
            "AND tx < pg_snapshot_xmin(pg_current_snapshot()))"
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.pool.acquire() as connection:
            while True:
                if not await connection.fetchval(query):
                    return
                self._sequencer_wake.set()
                if loop.time() > deadline:
                    raise TimeoutError("events are not being sequenced; is the sequencer running?")
                await asyncio.sleep(0.002)

    # ------------------------------------------------------------------ consuming

    async def subscribe(
        self,
        scope: Scope,
        pattern: str,
        *,
        group: str | None = None,
        start: StartFrom = "new",
        ack_wait: float = 30.0,
        retry: RetryPolicy | None = None,
    ) -> Subscription:
        if self._closed:
            raise BusClosedError("the event bus is closed")
        validate_pattern(pattern)
        if group is not None:
            validate_name(group, "group")
        name = group or f"_inbox.{new_ulid()}"
        key = f"{scope.tenant}/{scope.app}/{name}"
        policy = retry or RetryPolicy()
        await self._barrier()
        async with self.pool.acquire() as connection, connection.transaction():
            # Locking the sequencer row orders this against fan-out (see _sequence).
            position = await connection.fetchval(
                self._sql("SELECT position FROM {s}.sequencer WHERE id = 1 FOR UPDATE")
            )
            existing = await connection.fetchrow(
                self._sql("SELECT * FROM {s}.groups WHERE key = $1 FOR UPDATE"), key
            )
            if existing is not None:
                if existing["pattern"] != pattern:
                    raise SubscriptionConflictError(
                        f"group {group!r} subscribes to {existing['pattern']!r}, not {pattern!r}"
                    )
                await connection.execute(
                    self._sql("UPDATE {s}.groups SET members = members + 1 WHERE key = $1"), key
                )
                found = _Group(existing)
            else:
                physical = physical_pattern(scope.tenant, scope.app, pattern)
                row = await connection.fetchrow(
                    self._sql(
                        "INSERT INTO {s}.groups (key, tenant, app, name, pattern, physical, "
                        "durable, ack_wait, retry, members) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 1) RETURNING *"
                    ),
                    key,
                    scope.tenant,
                    scope.app,
                    name,
                    pattern,
                    physical,
                    group is not None,
                    ack_wait,
                    {
                        "max_deliveries": policy.max_deliveries,
                        "initial_backoff": policy.initial_backoff,
                        "max_backoff": policy.max_backoff,
                        "multiplier": policy.multiplier,
                        "jitter": policy.jitter,
                    },
                )
                found = _Group(row)
                await self._backfill(connection, found, start, position)
        member = _PostgresSubscription(self, found)
        self._members.append(member)
        return member

    async def _backfill(
        self, connection: Any, group: _Group, start: StartFrom, position: int
    ) -> None:
        if start == "new":
            return
        query = "SELECT position, subject, runid FROM {s}.events WHERE position IS NOT NULL"
        args: list[Any] = []
        if isinstance(start, int) and not isinstance(start, bool):
            query += " AND position > $1"
            args.append(start)
        elif isinstance(start, datetime):
            query += " AND time >= $1"
            args.append(start)
        rows = await connection.fetch(self._sql(query + " ORDER BY position"), *args)
        chosen = [
            r for r in rows if r["position"] <= position and matches(group.physical, r["subject"])
        ]
        if chosen:
            await connection.execute(
                self._sql(
                    "INSERT INTO {s}.deliveries (group_key, position, run_key) "
                    "SELECT $1, * FROM unnest($2::bigint[], $3::text[])"
                ),
                group.key,
                [r["position"] for r in chosen],
                [r["runid"] for r in chosen],
            )

    async def claim(self, group: _Group, member: str) -> Delivery | None:
        """Lease the next deliverable event in one statement (the hot path)."""
        while True:
            token = new_ulid()
            async with self.pool.acquire() as connection:
                row = await connection.fetchrow(
                    self._sql(_CLAIM), group.key, token, member, group.ack_wait
                )
                if row is None:
                    return None
                if row["attempt"] <= group.retry.max_deliveries:
                    stored = StoredEvent(row["position"], _event(row["body"]))
                    return _PostgresDelivery(self, group, stored, row["attempt"], token)
                # An expired lease that had used its last delivery: dead-letter it, try again.
                async with connection.transaction():
                    await self._dead_letter(
                        connection,
                        group,
                        row["position"],
                        row["attempt"] - 1,
                        "ack deadline exceeded",
                    )

    async def settle(self, group: _Group, position: int, token: str) -> None:
        """Ack: delete the delivery and wake members waiting behind it, in one statement."""
        async with self.pool.acquire() as connection:
            await connection.execute(
                self._sql(
                    "WITH gone AS (DELETE FROM {s}.deliveries WHERE group_key = $1 "
                    "AND position = $2 AND lease_token = $3 RETURNING run_key) "
                    "SELECT pg_notify('{s}_deliveries', '') FROM gone WHERE run_key IS NOT NULL"
                ),
                group.key,
                position,
                token,
            )
        self.notify()

    async def retry(
        self, group: _Group, position: int, token: str, delay: float | None, reason: str
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            attempt = await connection.fetchval(
                self._sql(
                    "SELECT attempt FROM {s}.deliveries WHERE group_key = $1 AND position = $2 "
                    "AND lease_token = $3 FOR UPDATE"
                ),
                group.key,
                position,
                token,
            )
            if attempt is None:
                return  # stale: the lease expired and the event was redelivered
            if attempt >= group.retry.max_deliveries:
                await self._dead_letter(connection, group, position, attempt, reason)
            else:
                wait = group.retry.backoff(attempt) if delay is None else delay
                await connection.execute(
                    self._sql(
                        "UPDATE {s}.deliveries SET lease_token = NULL, lease_owner = NULL, "
                        "lease_deadline = NULL, not_before = clock_timestamp() + "
                        "make_interval(secs => $3) WHERE group_key = $1 AND position = $2"
                    ),
                    group.key,
                    position,
                    wait,
                )
        await self._wake()

    async def extend(self, group: _Group, position: int, token: str) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                self._sql(
                    "UPDATE {s}.deliveries SET lease_deadline = clock_timestamp() + "
                    "make_interval(secs => $4) WHERE group_key = $1 AND position = $2 "
                    "AND lease_token = $3"
                ),
                group.key,
                position,
                token,
                group.ack_wait,
            )

    async def leave(self, group: _Group, member: str) -> None:
        """A member left: its unsettled deliveries go back to the queue immediately."""
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                self._sql(
                    "UPDATE {s}.deliveries SET lease_token = NULL, lease_owner = NULL, "
                    "lease_deadline = NULL, not_before = '-infinity' "
                    "WHERE group_key = $1 AND lease_owner = $2"
                ),
                group.key,
                member,
            )
            members = await connection.fetchval(
                self._sql(
                    "UPDATE {s}.groups SET members = greatest(members - 1, 0) WHERE key = $1 "
                    "RETURNING members"
                ),
                group.key,
            )
            if not group.durable and members == 0:
                await connection.execute(
                    self._sql("DELETE FROM {s}.groups WHERE key = $1"), group.key
                )
        self._members = [m for m in self._members if m.member != member]
        await self._wake()

    async def _wake(self) -> None:
        self.notify()
        async with self.pool.acquire() as connection:
            await connection.execute(f"SELECT pg_notify('{self.schema}_deliveries', '')")

    # ------------------------------------------------------------------ dead letters

    async def _dead_letter(
        self, connection: Any, group: _Group, position: int, attempts: int, reason: str
    ) -> None:
        body = await connection.fetchval(
            self._sql("SELECT body FROM {s}.events WHERE position = $1"), position
        )
        event = _event(body)
        letter_id = new_ulid()
        dead_at = datetime.now(UTC)
        await connection.execute(
            self._sql("DELETE FROM {s}.deliveries WHERE group_key = $1 AND position = $2"),
            group.key,
            position,
        )
        await connection.execute(
            self._sql(
                "INSERT INTO {s}.dead_letters (id, group_key, group_name, position, attempts, "
                "reason, dead_at, tenantid, appid) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)"
            ),
            letter_id,
            group.key,
            group.name,
            position,
            attempts,
            reason,
            dead_at,
            event.tenantid,
            event.appid,
        )
        if event.type == DLQ_EVENT_TYPE:
            return  # never cascade dead-letter notices
        payload = DeadLetterAdded(
            dead_letter=letter_id,
            group=group.name,
            event=event.id,
            event_type=event.type,
            attempts=attempts,
            reason=reason,
            dead_at=dead_at,
        )
        notice = event.derive(
            DLQ_EVENT_TYPE,
            payload.model_dump(mode="json"),
            source=f"aios://{event.tenantid}/system/fabric",
            principal="system:fabric",
        )
        # Stored directly (same transaction), so a notice about an event at the hop limit lands.
        await self._insert(connection, [(notice, physical_subject(notice))])

    def _in_scope(self, scope: Scope, tenant: str, app: str) -> bool:
        return scope.tenant in ("*", tenant) and scope.app in ("*", app)

    async def dead_letters(self, scope: Scope) -> list[DeadLetter]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                self._sql(
                    "SELECT d.*, e.body FROM {s}.dead_letters d "
                    "JOIN {s}.events e ON e.position = d.position ORDER BY d.dead_at, d.id"
                )
            )
        return [
            DeadLetter(
                id=row["id"],
                group=row["group_name"],
                stored=StoredEvent(row["position"], _event(row["body"])),
                attempts=row["attempts"],
                reason=row["reason"],
                dead_at=row["dead_at"],
            )
            for row in rows
            if self._in_scope(scope, row["tenantid"], row["appid"])
        ]

    async def _take(self, connection: Any, scope: Scope, letter_id: str) -> Any:
        row = await connection.fetchrow(
            self._sql("SELECT * FROM {s}.dead_letters WHERE id = $1 FOR UPDATE"), letter_id
        )
        if row is None or not self._in_scope(scope, row["tenantid"], row["appid"]):
            raise NotFoundError(f"no dead letter {letter_id!r} in scope")
        await connection.execute(self._sql("DELETE FROM {s}.dead_letters WHERE id = $1"), letter_id)
        return row

    async def redrive(self, scope: Scope, dead_letter_id: str) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._take(connection, scope, dead_letter_id)
            exists = await connection.fetchval(
                self._sql("SELECT 1 FROM {s}.groups WHERE key = $1"), row["group_key"]
            )
            if exists is None:
                raise NotFoundError(f"consumer group {row['group_name']!r} no longer exists")
            runid = await connection.fetchval(
                self._sql("SELECT runid FROM {s}.events WHERE position = $1"), row["position"]
            )
            await connection.execute(
                self._sql(
                    "INSERT INTO {s}.deliveries (group_key, position, run_key) VALUES ($1, $2, $3)"
                ),
                row["group_key"],
                row["position"],
                runid,
            )
        await self._wake()

    async def discard(self, scope: Scope, dead_letter_id: str) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._take(connection, scope, dead_letter_id)

    # ------------------------------------------------------------------ replay

    async def replay(
        self,
        scope: Scope,
        pattern: str = ">",
        *,
        after: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[StoredEvent]:
        physical = physical_pattern(scope.tenant, scope.app, pattern)
        await self._barrier()
        conditions = ["position IS NOT NULL", "position > $1"]
        args: list[Any] = [max(after or 0, 0)]
        for column, value in (("tenantid", scope.tenant), ("appid", scope.app)):
            if value != "*":
                args.append(value)
                conditions.append(f"{column} = ${len(args)}")
        if run_id is not None:
            args.append(run_id)
            conditions.append(f"runid = ${len(args)}")
        if since is not None:
            args.append(since)
            conditions.append(f"time >= ${len(args)}")
        if until is not None:
            args.append(until)
            conditions.append(f"time < ${len(args)}")
        query = self._sql(  # only column names and placeholders are interpolated
            "SELECT position, subject, body FROM {s}.events WHERE "  # noqa: S608
            + " AND ".join(conditions)
            + " ORDER BY position LIMIT 1000"
        )
        while True:
            async with self.pool.acquire() as connection:
                rows = await connection.fetch(query, *args)
            for row in rows:
                if matches(physical, row["subject"]):
                    yield StoredEvent(row["position"], _event(row["body"]))
            if len(rows) < 1000:
                return
            args[0] = rows[-1]["position"]
