# ADR-013: PostgreSQL stays the record, and a relay copies it to NATS JetStream for delivery

- **Status:** Proposed — drafted 7 Nov 2026 for the owner to decide, after the
  [Phase 3 review](../reviews/phase-3.md) found it overdue (finding 4). Nothing is built yet.
- **Date:** 2026-11-07
- **Deciders:** Project owner, tech lead, platform engineer
- **Requirements:** FR-EVT-05, 07, 09, 13, 14, 15; NFR-01, 02, 05, 06, 09, 10; WP 4.1, WP 4.9;
  M4 exit 2; ADR-003, ADR-004, ADR-005

## Context

The fabric runs on PostgreSQL (ADR-004). Producers insert events into `events` inside their own
transaction, a sequencer gives every committed event a gapless `position` behind the read horizon,
and it fans each position out to one `deliveries` row per matching durable group. Members claim
rows with `FOR UPDATE SKIP LOCKED`, skipping a row while an earlier row of the same `runid` in the
same group is unsettled, which is how per-run order holds (ADR-005). Ephemeral subscriptions tail
the log by position in their own process. It meets NFR-01 with headroom: p99 18–29 ms at
1,000 events/s through the full adapter.

FR-EVT-15 asks for NATS JetStream next (P1), behind the same port and passing the same conformance
suite, and NFR-02 asks it for p99 ≤ 20 ms at 10,000 events/s. WP 4.1 recommends that the
PostgreSQL outbox stays the system of record and that a relay publishes to JetStream. Three things
in the code make that more than a preference:

- **Transactions.** Three stores commit state and events together through `publish_in(connection,
  …)`, at eight call sites: the control plane, Human OS and the model ledger (FR-EVT-07). Publishing straight to
  JetStream would turn each of these into a dual write.
- **Reading a run.** About thirty call sites read the log through `replay(scope, run_id=…)` and
  `latest(…)`: the engine folds a run's state from it, the kernel checks whether a task has already
  settled, the Model Kernel and Tool OS answer recorded calls from it (NFR-06), and replay and fork
  depend on it (ADR-021). JetStream reads a stream by sequence and subject. The run id isn't in the
  subject (ADR-003), and putting it there would make a subject per run.
- **Per-run order for competing consumers.** JetStream has no per-key ordering inside a consumer.
  Its documentation says a redelivered message arrives after later ones unless `MaxAckPending`
  is 1.

Three more requirements land with the same work package: claim-check for payloads above 256 KB
(FR-EVT-13), retention per event domain with archival and legal hold (FR-EVT-14, neither built on
PostgreSQL yet), and a migration that changes nothing in any App.

## Decision

**1. PostgreSQL stays the system of record.** Every event is committed to `events` first, in the
producer's transaction, and gets its position from the sequencer, exactly as now. Replay, `latest`,
run folds, dead letters, retention, archival and legal hold all read PostgreSQL. JetStream holds a
copy for delivery, and no AIOS state lives only there. If JetStream loses data, the copy is rebuilt
from PostgreSQL, so no acknowledged event can be lost to NATS (NFR-05).

**2. One relay copies the log to JetStream, in position order, once.** It runs in the sequencer's
loop under the same advisory lock, so there is one relay per installation and its failover is the
sequencer's. After each sequenced batch it publishes those positions to one stream, `AIOS_EVENTS`.

- Each message is the envelope in CloudEvents structured JSON, as stored. Its headers are
  `Nats-Msg-Id` (the event id, so a repeated publish within the duplicate window is dropped) and
  `Aios-Position`.
- Each publish also carries `Nats-Expected-Last-Sequence`: the stream sequence of the position
  before it. JetStream stores the message only if that matches. During a failover, a second relay
  can't interleave or duplicate: its publish fails, and it reads the stream's last `Aios-Position`
  and carries on from there.
- So stream sequence and position differ by a constant, the base the relay records in PostgreSQL
  when it creates the stream. "Start after position *n*" and "start at a time" map onto
  JetStream's own start options.
- Whether the relay publishes message by message, as an atomic batch (NATS 2.12) or as a fast
  batch (2.14) is measured in the spike, not decided here.

**3. Subjects keep the topic grammar.** A message's subject is
`aios.<tenant>.<app>.<partition>.<logical topic>[.<route>]`. That is the physical subject from
ADR-003 with a prefix and a partition token. ADR-003 was written to be NATS-compatible, so patterns
translate token for token, with `*` standing in for the partition. The partition is
`p` + (a hash of `runid` mod *P*), or of the event id when there is no run. *P* is fixed when the
stream is created (default 16). It costs nothing now and is what durable groups need later (point 5).

**4. Ephemeral subscriptions move to JetStream first.** These are `group=None` subscriptions:
broadcast, `request()` replies, live run views, the console's event stream, and the waits behind
`/openai/v1` and `/mcp` (ADR-025). Each becomes an ephemeral JetStream consumer with explicit acks
and an inactivity threshold, and AckWait, MaxDeliver and BackOff taken from the subscription's
`RetryPolicy`. A private group has exactly one member, so the adapter enforces per-run order in that
process exactly, without holding throughput to `MaxAckPending: 1`. While NATS is unreachable, these
subscriptions fall back to tailing PostgreSQL as they do today.

Durable groups stay on PostgreSQL's `deliveries` in this step. That keeps per-run order, leases and
dead letters exactly as they are, and it is a configuration a smaller team can stop at
(plan §9.2).

**5. Durable groups move to JetStream only after a spike passes.** The spike has to pass all
40 conformance cases, NFR-02 through the relay, and M4 exit 2 (losing a NATS node under load loses
no events and duplicates no write-tool effects). It tries two designs:

| Design | How per-run order holds | What it costs |
|---|---|---|
| **A. Serial partitions** | One durable consumer per (group, partition), `MaxAckPending: 1`. Order holds per partition, which is stricter than per run | A long task holds its whole partition, so a dispatch group's parallelism is capped at *P*. A poison event blocks its partition, not just its run, until it is dead-lettered. Consumers = groups × *P* |
| **B. Pinned partitions with a run gate** | The same partitions, with `MaxAckPending` high and the `pinned_client` priority-group policy (NATS 2.11), so one member works a partition at a time and orders each run in memory | Keeps parallelism across runs. But NATS says a pin doesn't give one client sole ownership, so order can break at a handover, when redeliveries arrive after later messages. `nats-py` sets the pin fields but doesn't implement the pinning loop, so AIOS would have to |

Design A could suit short platform handlers (engine, ledger, audit) and fail long agent tasks, so
the spike measures both kinds of group. If neither design passes, durable groups stay on
PostgreSQL. That is safe, because the record and the claims stay correct, and NFR-02 then rests on
PostgreSQL's claim path. Groups on JetStream checkpoint their acknowledged floor to PostgreSQL
every few seconds, so a lost stream is rebuilt from the oldest checkpoint. Dead letters stay in
PostgreSQL: on the last attempt the adapter terminates the message (`term`) and writes the dead
letter and `system.dlq.added` in one transaction, so inspect, redrive and discard don't change.

**6. Claim-check is in the port, for every backend (FR-EVT-13).** `EventBus.publish` validates the
payload first. If the serialized `data` is larger than 256 KB (configurable), it stores the bytes
in a `BlobStore` and publishes the event with the CloudEvents `dataref` attribute and no `data`.
`dataref` is a documented CloudEvents extension, titled "Claim Check Pattern".

- The reference is `aios://<tenant>/apps/<app>/blobs/sha256:<hex>`, content-addressed, so the
  digest proves integrity. The audit chain covers the reference, and so the content too.
- Deliveries and replay hand subscribers the event with `data` filled in again, read under the
  subscriber's scope and checked against the digest. Agent code never sees a reference.
- `BlobStore` adapters: a PostgreSQL `blobs` table (the default on one node, keeping principle
  P10, and outside the hot `events` table), S3-compatible object storage (MinIO in the compose
  stack), and a folder for tests. With PostgreSQL blobs, `publish_in` writes the blob in the same
  transaction. With object storage, it uploads before the commit, and a sweep removes blobs that
  no event references after a day.
- A blob's retention, legal hold and erasure follow its events (NFR-09).

**7. Retention, archival and legal hold live in PostgreSQL (FR-EVT-14).** `events` is
list-partitioned by retention class, and range-partitioned by month inside each class. A
configuration maps each event domain (the first topic token: `model`, `tool`, `approval`, …) to a
class with a duration, and a tenant can shorten it within the installation's bounds. A month that
ages out is first copied to object storage: gzipped JSON Lines with a manifest holding the count,
the first and last position and a SHA-256. The copy is verified, and only then is the partition
dropped. An archived month can be loaded back for an audit or a replay.

A legal hold names a tenant and optionally an App, a run or a domain. It is recorded as an event,
and placing or lifting one needs `tenant-admin`. While a hold touches a partition, that partition
isn't dropped and its archive isn't deleted. The audit log is a table of its own (FR-SEC-08), so
dropping event partitions doesn't break its chain.

JetStream retention is operational, not a compliance setting: limits retention, `MaxAge` 7 days
by default, `MaxBytes` set, discard old. `MaxAge` must stay well under the 30 days NFR-09 gives
erasure, so copies age out of NATS without anyone deleting messages there. A durable group that
starts before the stream's first sequence is backfilled from PostgreSQL, then handed to JetStream.
Interest and work-queue retention don't fit: they delete what a new group or a replay still needs.

**8. Every backend passes one suite.** `aios.testing.fabric` (40 cases) runs against the relayed
adapter in CI every night against a real `nats-server`, and against memory and PostgreSQL on every
commit, as the plan's testing strategy says. New cases join the shared suite, so all three
adapters pass them:

- claim-check round trip, a tampered blob refused, and a reference that never reaches a subscriber;
- a group starting at a position the stream no longer holds;
- a subscriber that sees PostgreSQL's order whatever NATS does.

Relay-only cases go in `tests/conformance/test_nats_bus.py`:

- killing the relay mid-batch duplicates and reorders nothing;
- two relays at once interleave nothing;
- deleting the stream loses no acknowledged event;
- an outage falls back to PostgreSQL and catches up.

`tests/chaos/` covers NATS node loss under load (M4 exit 2), and `benchmarks/fabric_bench.py
--adapter nats` measures NFR-02.

**9. Migration changes configuration, not Apps.** Apps, agents and workflows see only the port and
the topic grammar. The steps:

1. Run NATS (a compose profile, Helm in WP 4.3).
2. Set `AIOS_NATS_URL`. The relay creates the stream and starts at the current position, or an
   earlier one given.
3. Restart processes. Ephemeral subscriptions use JetStream.
4. After the spike, move durable groups one at a time. A group stops claiming on PostgreSQL, and its
   JetStream consumer starts at the group's oldest unsettled position, so anything in flight is
   delivered again, which idempotent consumers absorb (ADR-005).

Rolling back is the same in reverse: PostgreSQL holds every position, so a group's `deliveries`
rows can be recreated from any position. Without `AIOS_NATS_URL`, nothing changes.

## Alternatives considered

| Option | Trade-off | Why not chosen |
|---|---|---|
| **JetStream as the system of record**, with a native adapter that publishes straight to the stream | Lowest latency; one hop; NATS's own replication for durability | Every `publish_in` becomes a dual write, or each store needs its own outbox. Replay by run needs an index outside NATS. Run state and its events would sit in two stores with separate backups. Row-level security has no equivalent, so tenant isolation would rest on subject prefixes alone |
| **Outbox only for transactional publishes**, with everything else straight to JetStream | Less relay traffic | Two paths give two orders, so positions stop being one sequence, and replay and the run fold break |
| **Logical decoding (CDC) as the relay's source** instead of the sequencer | Commit order from the WAL with no read-horizon wait, and possibly lower latency | It needs a replication-protocol client. `asyncpg`'s support is unproven and `psycopg` is LGPL (ADR-012). Replication slots can fill the disk if the relay stops. It is the fallback if the sequencer can't meet NFR-02 |
| **Debezium or Kafka Connect** as the relay | Proven CDC tooling | A JVM service and a connector framework, to move one table to one stream |
| **Stay on PostgreSQL only** | One stateful system; no relay | FR-EVT-15 asks for JetStream (P1), and R4 says PostgreSQL may hit limits. It stays the right setting for one node and for small teams, and this design keeps it the default |
| **Run id in the subject** | Per-run subjects would let JetStream filter a run | A subject per run, ADR-003's grammar broken, and consumers filtering millions of subjects |
| **Kafka or Redpanda now** | Per-key partition order is native | FR-EVT-15 puts it at P2, on demand. A JVM or a second broker before the first is proven. The port leaves the door open |
| **Interest or work-queue retention** on the stream | Smaller stream | Deletes what new groups and replay need. PostgreSQL is the history anyway |

## Consequences

- **The relay can't be faster than the sequencer.** Publish-to-delivery through JetStream is the
  sequencer's latency (about 14 ms at p50 in the ADR-004 spike, mostly wake-up and round trips)
  plus a relay hop. NFR-02's p99 of 20 ms at 10,000 events/s therefore needs the sequencer to keep
  up at ten times its measured load. The spike measures that first. If it misses, the options are
  tuning (batch size, wake-up), CDC as the relay's source, or the owner restating NFR-02. **This is
  the decision most worth the owner's attention.**
- NATS becomes a second stateful system in HA deployments. It holds nothing PostgreSQL doesn't, so
  its backups matter for recovery time, not for durability.
- A NATS outage slows live views and replies (they fall back to PostgreSQL). It never loses or
  reorders events, and durable work goes on.
- Only AIOS processes connect to NATS, with one platform credential (an NKey or JWT for an `AIOS`
  account, over TLS). Agent code never does: it builds no subjects (FR-EVT-04), and from ADR-014 on
  it holds no bus at all. Tenant isolation on NATS is the adapter's subject prefixing, as the
  conformance suite's isolation cases check.
- Large payloads leave the `events` table for every backend, which also helps PostgreSQL.
- Monthly partitions, retention classes and archival are PostgreSQL work. They were already owed by
  ADR-004 decision 1, and they don't depend on NATS.
- *P* is fixed per stream. Changing it means a new stream, which the relay fills from PostgreSQL.
- If durable groups never move, NATS serves broadcast only, and the plan's "low-latency fan-out"
  covers ephemeral subscribers. That is an honest outcome to report at M4.

## Implementation

Nothing is built. If accepted, WP 4.1 changes:

- **`src/aios/fabric/nats.py` (new):** `RelayedEventBus(EventBus)` wraps `PostgresEventBus` and
  sends `subscribe(group=None)`, and later durable groups, to JetStream. `JetStreamRelay` holds the
  stream setup, the base and the publish loop.
- **`src/aios/fabric/postgres.py`:**
  - a hook after `_sequence` for the relay;
  - a `relay_state` table (stream, base, relayed position, group checkpoints);
  - `events` partitioned by retention class and month, with archival and a `holds` table;
  - a `blobs` table.
- **`src/aios/fabric/port.py`:** claim-check in `publish`, resolution for deliveries and replay.
  `BlobStore` goes in `src/aios/core/ports.py`, so `fabric` still depends only on `core` (ADR-001),
  with adapters in `src/aios/fabric/blobs.py`.
- **`src/aios/core/envelope.py`:** the `dataref` attribute, a compatible change under ADR-002, in
  `specs/event-envelope.md` and its schema.
- **`src/aios/testing/fabric.py`:** the new shared cases, plus `tests/conformance/test_nats_bus.py`,
  `tests/chaos/` and `benchmarks/fabric_bench.py --adapter nats`.
- **`pyproject.toml`:** `nats-py` (Apache-2.0) as an optional extra.
- **Elsewhere:** `--nats-url` / `AIOS_NATS_URL` in `src/aios/cli/main.py`, a `nats` service under
  a compose profile, and a nightly CI job with `nats-server`.

The spike comes first, in `spikes/`, as ADR-004's did. Its results are added here before the owner
decides point 5.

## Sources

Checked on 7 Nov 2026.

- `nats-server` v2.15.2 and v2.14.9 are the latest stable releases (26 Oct 2026); Apache-2.0:
  [releases](https://github.com/nats-io/nats-server/releases), [repository](https://github.com/nats-io/nats-server).
  NATS moved to Graduated in the CNCF on 2 Nov 2026: [CNCF project page](https://www.cncf.io/projects/nats/).
  The 2025 trademark dispute ended with the marks assigned to the Linux Foundation and development
  continuing under Apache-2.0:
  [CNCF announcement, 1 May 2025](https://www.cncf.io/announcements/2025/05/01/cncf-and-synadia-align-on-securing-the-future-of-the-nats-io-project/).
- Releases:
  - [2.11](https://nats.io/blog/nats-server-2.11-release/) (20 Mar 2025): consumer priority
    groups and pinning, per-message TTL.
  - [2.12](https://nats.io/blog/nats-server-2.12-release/) (22 Sep 2025): atomic batch publish.
  - [2.14](https://nats.io/blog/nats-server-2.14-release/) (30 Apr 2026): fast batch publish.
  - [2.15](https://nats.io/blog/nats-server-2.15-release/) (17 Sep 2026): reliability and
    operations, few new features.
- [Priority groups](https://docs.nats.io/learn/jetstream/priority-groups): the pin doesn't give
  sole ownership, and the Python client only sets the configuration fields.
- [Acknowledgment](https://docs.nats.io/learn/jetstream/acknowledgment): AckWait defaults to 30 s,
  MaxDeliver is unlimited by default, BackOff, `term`, and redeliveries arriving after later
  messages unless `MaxAckPending` is 1.
- [Headers](https://docs.nats.io/reference/jetstream/api/headers): `Nats-Msg-Id`,
  `Nats-Expected-Last-Sequence`. [Streams](https://docs.nats.io/nats-concepts/jetstream/streams):
  a two-minute duplicate window by default. [Retention](https://docs.nats.io/learn/jetstream/retention-policies).
- [`nats-py`](https://pypi.org/project/nats-py/) 2.16.0, 16 Sep 2026, Apache-2.0.
- [CloudEvents `dataref`](https://github.com/cloudevents/spec/blob/main/cloudevents/extensions/dataref.md),
  "Claim Check Pattern", a documented extension.
