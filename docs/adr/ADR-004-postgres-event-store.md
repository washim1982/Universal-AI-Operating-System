# ADR-004: PostgreSQL event store and delivery design

- **Status:** Accepted — 29 Oct 2026, project owner (WP 0.4), after the read-horizon spike
- **Date:** 2026-10-28 (proposed), 2026-10-29 (decided)
- **Deciders:** Tech lead, platform engineer
- **Requirements:** FR-EVT-05, 07, 08, 09, 15; NFR-01, NFR-05, NFR-06; requirements §8.1

## Context

The MVP fabric runs on PostgreSQL 17 so that the whole OS is one database plus the worker
(principle P10). `LISTEN/NOTIFY` alone loses notifications and caps payloads at 8 KB, and a
naive `bigserial`-ordered events table silently skips events when transactions commit out of
order. The adapter must pass the same conformance suite as the in-memory bus, including
per-run ordering in consumer groups and an integer `position` for replay.

## Decision

1. **Outbox = log.** Producers insert into `events` inside their own transaction (FR-EVT-07).
   Columns: `tx xid8 DEFAULT pg_current_xact_id()`, `seq bigint` (from a sequence), `subject`,
   the envelope columns (`id`, `type`, `tenantid`, `appid`, `runid`, `time`, …) and `body jsonb`.
   Partitioned by month on `time`; old partitions archived to object storage (FR-EVT-14, P1).
2. **Sequencer assigns positions.** One sequencer (leader via `pg_try_advisory_lock`) reads rows
   from transactions older than `pg_snapshot_xmin(pg_current_snapshot())` — the read horizon —
   in `(tx, seq)` order and assigns a gapless `position bigint`. Only sequenced rows are
   visible to consumers, so commit-order races cannot skip events. Wake-up is a payload-free
   `NOTIFY` from an insert trigger, with a 50–100 ms polling fallback.
3. **Fan-out to durable groups** happens in the sequencer's transaction: for each new position
   it inserts one `deliveries` row per matching group, using the same Python matcher as
   `aios.core.topics` (one implementation of the semantics).
4. **Claiming** uses `SELECT … FOR UPDATE SKIP LOCKED` on `deliveries` with a lease column, and
   skips rows whose `run_key` has an earlier unsettled row in the same group (per-run order).
   Ack deletes the row; nack sets `attempt`/`not_before`; past `max_deliveries` the row moves to
   `dead_letters` and `system.dlq.added` is appended in the same transaction.
5. **Ephemeral (broadcast) subscriptions** tail the log by position from memory in the
   subscribing process; they need no rows, because they do not survive the process anyway.
6. **Replay** reads `events` by position, time range or `runid` under the subscriber's scope.
7. Row-level security on `tenantid` for every table (FR-SEC-04, WP 2.2).

## Alternatives considered

| Option | Why not |
|---|---|
| `bigserial` order only | Skips events from transactions that commit out of order |
| Expose `(tx, seq)` as the position | Correct, but positions are pairs; the port and every adapter would carry a composite type |
| LISTEN/NOTIFY as the bus | Loses messages with no listener; 8 KB payload cap; commit serialization under load |
| Logical replication / CDC | Strong ordering, but heavier operations and permissions; reconsider for the NATS relay (ADR-013) |
| Kafka/NATS from day one | Second stateful system before the first demo (implementation plan §3) |

## Spike results (29 Oct 2026)

`spikes/pg_sequencer.py` implemented decisions 1, 2 and 5 (log, read-horizon sequencer,
position tailing) against PostgreSQL 17.6 from `deploy/compose`, with `asyncpg`. Details in
[spikes/README.md](../../spikes/README.md#adr-004-read-horizon-sequencer-29-oct-2026).

- **The problem is real.** A reader tailing by `seq` skipped 1,001 of 2,235 committed events
  (run 2: 1,067 of 2,308) under 8 concurrent writers with in-transaction delays and rollbacks.
- **The sequencer fixes it.** In the same runs every committed event got exactly one gapless
  position, and a consumer tailing by position saw each event exactly once. While an older
  transaction is open, nothing behind it is sequenced.
- **NFR-01 holds with headroom.** At 1,000 events/s for 20 s, publish-to-delivery was p50
  14.0 / 14.3 ms and p99 20.6 / 21.2 ms (target p50 ≤ 20 ms, p99 ≤ 100 ms) on a Windows laptop
  through Docker Desktop.

Not yet measured: the `deliveries` claim path (decisions 3–4), sequencer failover, and Linux.
WP 1.1 re-measures NFR-01 through the full adapter in the nightly benchmark; if it misses, a
superseding ADR revisits the design.

## Consequences

- Publish-to-delivery latency includes one sequencer hop (about 14 ms at p50 in the spike,
  most of it wake-up and round trips rather than database work).
- The sequencer is a single writer; it must be cheap to fail over (advisory lock, idempotent
  batches keyed by `(tx, seq)`).
- Long-running transactions hold back the read horizon; the adapter exports horizon lag as a
  metric and alerts on it (`system.consumer.lagging`).

## Implementation

WP 1.1: `src/aios/fabric/postgres/` on `asyncpg` (Apache-2.0; `psycopg` is LGPL-3.0, see
ADR-012), migrations, `tests/conformance` parametrized with a testcontainers PostgreSQL,
`benchmarks/fabric_bench.py --adapter postgres`. The spike's SQL (`spikes/pg_sequencer.py`)
is the starting point.
