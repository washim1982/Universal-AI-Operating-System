"""ADR-004 spike: a gapless event log on PostgreSQL with a read-horizon sequencer.

Validates the two assumptions that ADR-004 rests on before WP 1.1 builds the adapter:

1. **Correctness.** Ordering by a ``bigserial`` skips events when transactions commit out of
   order. Assigning positions only to rows older than the snapshot ``xmin`` (the read horizon),
   in ``(tx, seq)`` order, gives every committed event exactly one gapless position, and a
   consumer tailing by position never misses one.
2. **Latency.** Publish-to-delivery through the sequencer (payload-free NOTIFY wake-ups plus a
   polling fallback) meets NFR-01: p50 <= 20 ms, p99 <= 100 ms at 1,000 events/s.

Throwaway code: WP 1.1 turns the design into ``aios.fabric.postgres`` with the conformance
suite. Run it against a disposable database (it drops and recreates the ``spike`` schema)::

    uv run python spikes/pg_sequencer.py --dsn postgresql://aios:XXXXXX@127.0.0.1:55450/aios
"""
# pyright: basic

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

import asyncpg

SCHEMA = """
DROP SCHEMA IF EXISTS spike CASCADE;
CREATE SCHEMA spike;

CREATE TABLE spike.events (
    tx       xid8   NOT NULL DEFAULT pg_current_xact_id(),
    seq      bigint GENERATED ALWAYS AS IDENTITY,
    position bigint UNIQUE,
    id       text   NOT NULL,
    body     jsonb  NOT NULL DEFAULT '{}'
);
CREATE INDEX events_unsequenced ON spike.events (tx, seq) WHERE position IS NULL;

CREATE TABLE spike.sequencer (id int PRIMARY KEY CHECK (id = 1), position bigint NOT NULL);
INSERT INTO spike.sequencer VALUES (1, 0);

-- Payload-free wake-up, once per inserting statement; delivered when the producer commits.
CREATE FUNCTION spike.wake_sequencer() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('aios_events', '');
    RETURN NULL;
END $$;
CREATE TRIGGER events_wake AFTER INSERT ON spike.events
    FOR EACH STATEMENT EXECUTE FUNCTION spike.wake_sequencer();
"""

# One statement, so one transaction: take every unsequenced row from transactions older than
# the read horizon, in (tx, seq) order, and give the batch the next gapless positions.
SEQUENCE = """
WITH ready AS (
    SELECT tx, seq FROM spike.events
    WHERE position IS NULL AND tx < pg_snapshot_xmin(pg_current_snapshot())
    ORDER BY tx, seq
    LIMIT $1
    FOR UPDATE
), numbered AS (
    SELECT tx, seq, row_number() OVER (ORDER BY tx, seq) AS n FROM ready
), advanced AS (
    UPDATE spike.sequencer SET position = position + (SELECT count(*) FROM numbered)
    WHERE id = 1
    RETURNING position - (SELECT count(*) FROM numbered) AS base
)
UPDATE spike.events AS e SET position = advanced.base + numbered.n
FROM numbered, advanced
WHERE e.tx = numbered.tx AND e.seq = numbered.seq
RETURNING e.position
"""

INSERT = "INSERT INTO spike.events (id) VALUES ($1)"
TAIL = "SELECT position, id FROM spike.events WHERE position > $1 ORDER BY position LIMIT 5000"
NAIVE_TAIL = "SELECT seq, id FROM spike.events WHERE seq > $1 ORDER BY seq"
POLL_SECONDS = 0.05  # fallback when a NOTIFY is missed or the horizon is held back


async def _wait(event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), seconds)


async def _listening(dsn: str, channel: str) -> tuple[asyncpg.Connection, asyncio.Event]:
    conn = await asyncpg.connect(dsn)
    wake = asyncio.Event()
    await conn.add_listener(channel, lambda *_: wake.set())
    return conn, wake


async def sequencer(dsn: str, stop: asyncio.Event, batch: int = 5000) -> int:
    """Sequence until ``stop`` is set and nothing is left; returns the number of rows."""
    conn, wake = await _listening(dsn, "aios_events")
    total = 0
    try:
        while True:
            wake.clear()
            rows = await conn.fetch(SEQUENCE, batch)
            if rows:
                total += len(rows)
                await conn.execute("SELECT pg_notify('aios_sequenced', '')")
                continue
            if stop.is_set():
                return total
            await _wait(wake, POLL_SECONDS)
    finally:
        await conn.close()


async def consumer(
    dsn: str, expected: int, on_event: Callable[[str], None], stop: asyncio.Event
) -> list[int]:
    """Tail the log by position, as broadcast subscribers will; returns positions seen."""
    conn, wake = await _listening(dsn, "aios_sequenced")
    seen: list[int] = []
    try:
        while len(seen) < expected and not (stop.is_set() and not wake.is_set()):
            wake.clear()
            rows = await conn.fetch(TAIL, seen[-1] if seen else 0)
            for row in rows:
                seen.append(row["position"])
                on_event(row["id"])
            if not rows:
                await _wait(wake, POLL_SECONDS)
    finally:
        await conn.close()
    return seen


async def reset(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SCHEMA)
    finally:
        await conn.close()


# --------------------------------------------------------------------------- correctness


async def out_of_order_commit(dsn: str) -> dict[str, object]:
    """The failure ADR-004 fixes, step by step, then the sequencer's answer."""
    await reset(dsn)
    first, second, reader = [await asyncpg.connect(dsn) for _ in range(3)]
    try:
        slow = first.transaction()
        await slow.start()
        await first.execute(INSERT, "a")  # gets the lower xid and seq ...
        await second.execute(INSERT, "b")  # ... but this one commits first
        naive = await reader.fetch(NAIVE_TAIL, 0)
        naive_cursor = max(row["seq"] for row in naive)
        sequenced_while_open = await reader.fetch(SEQUENCE, 100)
        await slow.commit()
        naive_after = await reader.fetch(NAIVE_TAIL, naive_cursor)
        sequenced = await reader.fetch(SEQUENCE, 100)
        order = await reader.fetch("SELECT id FROM spike.events ORDER BY position")
        return {
            "naive_reader_saw": [row["id"] for row in naive],
            "naive_reader_missed": "a" if not naive_after else None,
            "sequenced_while_a_open": len(sequenced_while_open),
            "positions_after_commit": [row["position"] for row in sequenced],
            "position_order": [row["id"] for row in order],
        }
    finally:
        for conn in (first, second, reader):
            await conn.close()


async def stress(dsn: str, writers: int, transactions: int) -> dict[str, object]:
    """Concurrent writers with random in-transaction delays (forcing out-of-order commits),
    one sequencer, one position-tailing consumer and one naive seq-tailing reader."""
    await reset(dsn)
    committed: set[str] = set()
    delivered: list[str] = []
    stop = asyncio.Event()
    pool = await asyncpg.create_pool(dsn, min_size=writers, max_size=writers)

    async def writer(index: int) -> None:
        for n in range(transactions):
            ids = [f"w{index}-{n}-{k}" for k in range(random.randint(1, 3))]
            rollback = random.random() < 0.05
            async with pool.acquire() as conn:
                tx = conn.transaction()
                await tx.start()
                for event_id in ids:
                    await conn.execute(INSERT, event_id)
                    await asyncio.sleep(random.uniform(0, 0.003))
                if rollback:
                    await tx.rollback()
                else:
                    await tx.commit()
                    committed.update(ids)

    async def naive_reader() -> set[str]:
        conn = await asyncpg.connect(dsn)
        cursor, seen = 0, set[str]()
        try:
            while not stop.is_set():
                for row in await conn.fetch(NAIVE_TAIL, cursor):
                    cursor = row["seq"]
                    seen.add(row["id"])
                await asyncio.sleep(0.001)
        finally:
            await conn.close()
        return seen

    sequencing = asyncio.create_task(sequencer(dsn, stop))
    naive = asyncio.create_task(naive_reader())
    consuming = asyncio.create_task(
        consumer(dsn, writers * transactions * 3, delivered.append, stop)
    )
    await asyncio.gather(*(writer(i) for i in range(writers)))
    stop.set()
    sequenced = await sequencing
    naive_seen = await naive
    await asyncio.sleep(POLL_SECONDS * 2)
    consuming.cancel()
    await asyncio.gather(consuming, return_exceptions=True)
    await pool.close()

    conn = await asyncpg.connect(dsn)
    try:
        positions = [r["position"] for r in await conn.fetch(
            "SELECT position FROM spike.events ORDER BY position"
        )]  # fmt: skip
    finally:
        await conn.close()
    return {
        "committed_events": len(committed),
        "sequenced": sequenced,
        "gapless": positions == list(range(1, len(positions) + 1)),
        "consumer_saw_each_exactly_once": sorted(delivered) == sorted(committed),
        "naive_reader_skipped": len(committed - naive_seen),
    }


# --------------------------------------------------------------------------- latency


@dataclass(frozen=True)
class Latency:
    events: int
    rate: float
    producers: int
    p50_ms: float
    p99_ms: float
    max_ms: float
    achieved_rate: float


async def latency(dsn: str, rate: float, seconds: float, producers: int) -> Latency:
    """Publish-to-delivery: from the start of the INSERT to the consumer reading the row."""
    await reset(dsn)
    total = int(rate * seconds)
    sent_at: dict[str, float] = {}
    latencies: list[float] = []
    stop = asyncio.Event()

    def received(event_id: str) -> None:
        latencies.append(time.perf_counter() - sent_at[event_id])

    pool = await asyncpg.create_pool(dsn, min_size=producers, max_size=producers)
    sequencing = asyncio.create_task(sequencer(dsn, stop))
    consuming = asyncio.create_task(consumer(dsn, total, received, stop))
    await asyncio.sleep(0.2)  # listeners are connected

    async def producer(index: int) -> None:
        interval = producers / rate
        start = time.perf_counter()
        for n in range(index, total, producers):
            event_id = f"e{n}"
            sent_at[event_id] = time.perf_counter()
            await pool.execute(INSERT, event_id)
            delay = start + (n // producers + 1) * interval - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

    started = time.perf_counter()
    await asyncio.gather(*(producer(i) for i in range(producers)))
    await asyncio.wait_for(consuming, timeout=30)
    elapsed = time.perf_counter() - started
    stop.set()
    await sequencing
    await pool.close()

    ordered = sorted(latencies)
    return Latency(
        events=total,
        rate=rate,
        producers=producers,
        p50_ms=round(statistics.median(ordered) * 1000, 2),
        p99_ms=round(ordered[round(0.99 * (len(ordered) - 1))] * 1000, 2),
        max_ms=round(ordered[-1] * 1000, 2),
        achieved_rate=round(total / elapsed, 1),
    )


async def main_async(args: argparse.Namespace) -> dict[str, object]:
    steps: list[tuple[str, Callable[[], Awaitable[object]]]] = [
        ("out_of_order_commit", lambda: out_of_order_commit(args.dsn)),
        ("stress", lambda: stress(args.dsn, writers=8, transactions=150)),
        ("latency", lambda: latency(args.dsn, args.rate, args.seconds, args.producers)),
    ]
    results: dict[str, object] = {}
    for name, step in steps:
        value = await step()
        results[name] = asdict(value) if isinstance(value, Latency) else value
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="ADR-004 read-horizon sequencer spike")
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--rate", type=float, default=1000.0, help="events/s (NFR-01: 1000)")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--producers", type=int, default=8)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main_async(args)), indent=2))


if __name__ == "__main__":
    main()
