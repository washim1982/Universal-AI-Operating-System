# ADR-020: Schema setup runs once per change, under one lock per schema

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-02
- **Deciders:** Tech lead
- **Requirements:** FR-SEC-06 (row-level security on every table), FR-EVT-07 (outbox); ADR-004

## Context

Every AIOS process sets up the database itself: the event bus when it connects, and each store
(installations, approvals, spend, memory, security) the first time it is used. Each setup took
its own advisory lock and ran `CREATE ... IF NOT EXISTS`, `ALTER TABLE ... ENABLE ROW LEVEL
SECURITY`, `DROP TRIGGER` / `CREATE TRIGGER` and `GRANT` on every start.

Those statements lock tables even when they change nothing. `CREATE INDEX IF NOT EXISTS` takes a
share lock that blocks writers, and `DROP TRIGGER` and `ALTER TABLE` take an exclusive lock that
blocks everyone. A process starting while others worked queued behind their transactions while
holding locks they needed next. A consumer's claim reads `events`, then waits for `deliveries`.
Setup holds `deliveries`, then waits for `events`. PostgreSQL broke the cycle by stopping one of
them with `deadlock detected`. The WP 3.2 browser check hit this when `aios platform key`
connected while an API and a worker started. The same thing happened whenever a worker restarted
under load. Setups with different lock keys could also interleave, and two of them updating the
same catalog row failed with `tuple concurrently updated`.

## Decision

**A setup already applied is not run at all.** Each component's setup is one text. Its SHA-256
digest is recorded in `schema_setup (component, digest)` in the same transaction that applies it.
On start, a process reads that table, and if its own digest is there, it does nothing else.
That takes no lock that traffic could hold up. Only a new schema, or new setup text (an upgrade),
runs the statements. Any digest once applied counts, so an old process restarting during a
rolling upgrade doesn't run its older setup again.

**Setups that do run are serialised and give way.** `PostgresEventBus.set_up` runs a setup in one
transaction, under one advisory lock per schema (`hashtext(schema)`). The lock covers the bus and
every store, so no two setups interleave. After taking it, the transaction checks the digest
again, then sets `lock_timeout` to 500 ms, below PostgreSQL's default 1 s `deadlock_timeout`. A
busy table makes the setup give way and try again, up to 30 times, instead of queueing writers
behind it or making PostgreSQL pick a victim from the traffic.

**Setup statements stay idempotent and as light as they can be.** Tables, indexes and functions
use `IF NOT EXISTS` / `OR REPLACE`. Row-level security and its policy are added only where they
are missing. Triggers use `CREATE OR REPLACE TRIGGER`, which takes a weaker lock than `DROP
TRIGGER`. The memory store creates an HNSW index only when `to_regclass` says it is missing.

The tenant role is cluster-wide, so it keeps its own lock and its check on every connect. That
check takes no table locks.

## Alternatives considered

| Option | Why not |
|---|---|
| One shared lock only | Setups no longer interleave, but each start still locks tables against traffic, so the claim deadlock remains |
| Retry on `deadlock detected` | PostgreSQL may stop the traffic's transaction rather than the setup's |
| A migration tool (Alembic, sqitch) run before the processes | A separate deploy step and a second definition of the schema. Worth revisiting when the schema needs real data migrations, not just additive setup |
| Version numbers instead of digests | Someone must remember to bump them. A digest changes whenever the text does |

## Consequences

- Starting a process against a set-up schema costs one query, and blocks nothing.
- Any change to a setup's text, even whitespace, runs that setup again once. That is safe, because
  every statement is idempotent.
- The setup text must stay idempotent and additive. A change that has to rewrite data needs a
  real migration step, which this ADR does not provide.
- Deleting a table by hand is not repaired on the next start. Delete its `schema_setup` rows too,
  or drop the schema.
- `schema_setup` is a platform table: row-level security on, no grant to tenants.

## Implementation

`PostgresEventBus.set_up` in `src/aios/fabric/postgres.py`. The bus calls it from `connect`, and
each store calls it from its first use: `src/aios/controlplane/postgres.py`,
`src/aios/human/postgres.py`, `src/aios/memory/postgres.py`, `src/aios/models/postgres.py` and
`src/aios/security/postgres.py`.

`tests/conformance/test_postgres_setup.py` runs on PostgreSQL:

- Several processes set up the schema and every store at once.
- A process restarts eight times while another publishes and three consumers claim. Before this
  change, that failed with `deadlock detected`.
- Starting takes no lock that a writer holding every table would block.
- A changed setup gives way to a busy table, then runs.

`test_every_table_is_covered` keeps checking row-level security on every table, `schema_setup`
included.
