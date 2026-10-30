# Spikes

Short experiments that settle an ADR before its work package starts. Spike code is throwaway:
it is linted but not type-checked, it is not part of the `aios` package, and the work package
rewrites it with tests. The results are recorded here and in the ADR.

| Spike | ADR | Question |
|---|---|---|
| [pg_sequencer.py](pg_sequencer.py) | [ADR-004](../docs/adr/ADR-004-postgres-event-store.md) | Does the read-horizon sequencer give a gapless, never-skipping log, fast enough for NFR-01? |
| [benchmarks/policy_bench.py](../benchmarks/policy_bench.py) and `aios.security.cedar` | [ADR-007](../docs/adr/ADR-007-policy-engine.md) | Can Cedar run on the hot path from Python? |

## ADR-004: read-horizon sequencer (29 Oct 2026)

Setup: PostgreSQL 17.6 (`pgvector/pgvector:0.8.0-pg17` from `deploy/compose`), Docker Desktop
on a Windows 11 laptop, `asyncpg` 0.32, Python 3.14. Two runs, results stable.

```bash
docker compose -p aios-spike -f deploy/compose/compose.yaml up -d --wait postgres
uv run python spikes/pg_sequencer.py --dsn postgresql://aios:XXXXXX@127.0.0.1:5432/aios
docker compose -p aios-spike -f deploy/compose/compose.yaml down -v
```

**Out-of-order commit, step by step.** Transaction A inserts event `a` (lower xid and seq) and
stays open; B inserts `b` and commits. A reader tailing by `seq` sees `b`, moves its cursor
past `a`'s seq, and never sees `a` after A commits. The sequencer assigns nothing while A is
open (A holds back the horizon), then gives `a` position 1 and `b` position 2.

**Stress.** 8 writers, 150 transactions each, 1–3 events per transaction, random delays inside
transactions and 5% rollbacks, with the sequencer, a position-tailing consumer and a naive
`seq`-tailing reader running throughout:

| | Run 1 | Run 2 |
|---|---|---|
| Committed events | 2,235 | 2,308 |
| Sequenced, gapless positions | 2,235, yes | 2,308, yes |
| Consumer saw every event exactly once | yes | yes |
| Events the naive `seq` reader skipped | 1,001 | 1,067 |

**Latency (NFR-01).** 1,000 events/s for 20 s from 8 producers. Each publish is its own
transaction; it's timed from the start of the INSERT until a consumer tailing by position
reads the row:

| | p50 | p99 | max | Achieved rate |
|---|---|---|---|---|
| Run 1 | 14.0 ms | 20.6 ms | 46.0 ms | 999.6/s |
| Run 2 | 14.3 ms | 21.2 ms | 37.1 ms | 999.5/s |
| NFR-01 target | ≤ 20 ms | ≤ 100 ms | | 1,000/s |

**Not covered:** the per-group `deliveries` table and its `SKIP LOCKED` claims (ADR-004
decisions 3–4); sequencer failover; partitions; Linux. WP 1.1's adapter must pass the
conformance suite, and the nightly benchmark re-measures NFR-01 through the full adapter.

## ADR-007: Cedar from Python (29 Oct 2026)

`cedarpy` 4.12.2 (k9securityio/cedar-py), the Python binding of the Rust Cedar library. It is
Apache-2.0 licensed, actively maintained (10 releases in 2026, the latest on 14 Oct, tracking
Cedar 4.x), and has wheels for CPython 3.10–3.14 on Linux, macOS and Windows. It is a
community project, not an AWS one; if it stalls, a thin PyO3 wrapper of our own over the
`cedar-policy` crate replaces it behind `aios.security.cedar` without touching callers.

The requirements §7.9 example and the Support Desk policy compile to Cedar and validate
against the AIOS Cedar schema (`aios.security.cedar`; 34 tests in `tests/unit/security/`).
`benchmarks/policy_bench.py` times 20,000 decisions over a mix of allow, deny, default-deny,
approval and delegation requests:

| | p50 | p99 | Decisions/s (one core) |
|---|---|---|---|
| Mixed requests | 0.20 ms | 1.06 ms | 3,900 |
| One allow decision | 0.11 ms | 0.80 ms | |
| …of which inside Cedar (entity parsing + request validation) | 0.09 ms | 0.67 ms | |
| …with a pre-parsed entity graph and no request validation | 0.04 ms | 0.46 ms | |

Approval and delegation requests evaluate twice. The AIOS code around Cedar costs about 2 µs.
Optimizing for WP 2.2: cache each tenant's stable entity graph (`Entities.with_added_json_str`
for per-request deltas) and validate requests once in debug builds.
