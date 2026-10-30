"""Fabric benchmark harness (WP 1.1, NFR-01 / NFR-02).

Measures publish-to-delivery latency and throughput for an EventBus adapter: each event is
timed from the start of ``publish`` until a consumer-group member receives it. NFR-01 is
measured on the PostgreSQL adapter (set ``AIOS_BENCH_DATABASE_URL``); NATS (WP 4.1) plugs in
through ``ADAPTERS`` too.

    uv run python benchmarks/fabric_bench.py --events 20000 --rate 1000 --consumers 4
    uv run python benchmarks/fabric_bench.py --adapter postgres --rate 1000 --events 20000
    uv run python benchmarks/fabric_bench.py --json results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from aios.core.ids import new_ulid
from aios.fabric import EventBus, InMemoryEventBus, Scope, Subscription
from aios.fabric.postgres import PostgresEventBus
from aios.testing import make_event


def _postgres() -> EventBus:
    dsn = os.environ.get("AIOS_BENCH_DATABASE_URL")
    if not dsn:
        raise SystemExit("set AIOS_BENCH_DATABASE_URL to benchmark the PostgreSQL adapter")
    return PostgresEventBus(dsn, schema=f"bench_{new_ulid().lower()}", drop_on_close=True)


ADAPTERS: dict[str, Callable[[], EventBus]] = {
    "memory": lambda: InMemoryEventBus(validate_payloads=False),
    "postgres": _postgres,
}


@dataclass(frozen=True)
class Result:
    adapter: str
    events: int
    target_rate: float | None
    consumers: int
    seconds: float
    throughput: float
    p50_ms: float
    p99_ms: float
    max_ms: float


def _percentile(sorted_values: list[float], fraction: float) -> float:
    index = min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


async def _consume(
    stream: Subscription,
    sent_at: dict[str, float],
    latencies: list[float],
    total: int,
    done: asyncio.Event,
) -> None:
    async for delivery in stream:
        latencies.append(time.perf_counter() - sent_at[delivery.event.id])
        await delivery.ack()
        if len(latencies) >= total:
            done.set()


async def _produce(
    bus: EventBus, indexes: range, rate: float | None, sent_at: dict[str, float]
) -> None:
    interval = 1 / rate if rate else 0.0
    started = time.perf_counter()
    for count, index in enumerate(indexes):
        event = make_event("bench.item.created", {"n": index}, tenant="bench", app="bench")
        sent_at[event.id] = time.perf_counter()
        await bus.publish(event)
        if interval:
            delay = started + (count + 1) * interval - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        elif count % 256 == 0:
            await asyncio.sleep(0)  # let consumers run


async def run(
    adapter: str, events: int, rate: float | None, consumers: int, producers: int = 1
) -> Result:
    scope = Scope("bench", "bench")
    sent_at: dict[str, float] = {}
    latencies: list[float] = []
    async with ADAPTERS[adapter]() as bus:
        streams = [
            await bus.subscribe(scope, "bench.>", group="bench", ack_wait=60)
            for _ in range(consumers)
        ]
        done = asyncio.Event()
        workers = [
            asyncio.create_task(_consume(s, sent_at, latencies, events, done)) for s in streams
        ]
        started = time.perf_counter()
        share = rate / producers if rate else None
        await asyncio.gather(
            *(_produce(bus, range(i, events, producers), share, sent_at) for i in range(producers))
        )
        await done.wait()
        elapsed = time.perf_counter() - started
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    ordered = sorted(latencies)
    return Result(
        adapter=adapter,
        events=events,
        target_rate=rate,
        consumers=consumers,
        seconds=round(elapsed, 3),
        throughput=round(events / elapsed, 1),
        p50_ms=round(statistics.median(ordered) * 1000, 3),
        p99_ms=round(_percentile(ordered, 0.99) * 1000, 3),
        max_ms=round(ordered[-1] * 1000, 3),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="EventBus latency and throughput benchmark")
    parser.add_argument("--adapter", choices=sorted(ADAPTERS), default="memory")
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--rate", type=float, default=None, help="events/s; default: flat out")
    parser.add_argument("--consumers", type=int, default=1)
    parser.add_argument("--producers", type=int, default=1, help="concurrent publishers")
    parser.add_argument("--json", type=Path, default=None, help="also write the result here")
    args = parser.parse_args()
    result = asyncio.run(run(args.adapter, args.events, args.rate, args.consumers, args.producers))
    print(json.dumps(asdict(result), indent=2))
    if args.json:
        args.json.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
