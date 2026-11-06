"""The hash-chained audit log (FR-SEC-08). PostgreSQL's copy is in tests/isolation."""

import asyncio
import dataclasses
from collections.abc import AsyncIterator

import pytest

from aios.fabric import InMemoryEventBus, Scope
from aios.security.audit import (
    GENESIS,
    AuditLog,
    AuditRecorder,
    InMemoryAuditLog,
    chained,
    check_chain,
)
from aios.testing import make_event


@pytest.fixture
def log() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus(validate_payloads=False) as bus:
        yield bus


async def _appended(log: AuditLog, count: int, tenant: str = "acme") -> None:
    for _ in range(count):
        await log.append(make_event("tool.call.completed", tenant=tenant))


async def test_each_entry_chains_to_the_one_before(log: InMemoryAuditLog) -> None:
    await _appended(log, 3)
    entries = await log.entries("acme")
    assert [e.seq for e in entries] == [1, 2, 3]
    assert entries[0].previous == GENESIS
    assert entries[1].previous == entries[0].hash
    assert entries[2].hash == chained(entries[1].hash, entries[2].entry)


async def test_an_intact_chain_verifies(log: InMemoryAuditLog) -> None:
    await _appended(log, 5)
    report = await log.verify("acme")
    assert report.ok
    assert report.entries == 5
    assert report.problem is None


async def test_an_empty_chain_verifies(log: InMemoryAuditLog) -> None:
    report = await log.verify("nobody")
    assert report.ok
    assert report.head == GENESIS


async def test_an_entry_keeps_the_whole_event(log: InMemoryAuditLog) -> None:
    event = make_event("tool.call.completed", {"tool": "crm.contact.get"}, run_id=None)
    await log.append(event)
    [entry] = await log.entries("acme")
    assert entry.event == event


async def test_recording_an_event_twice_adds_one_entry(log: InMemoryAuditLog) -> None:
    event = make_event("tool.call.completed")
    assert await log.append(event)
    assert not await log.append(event)
    assert (await log.verify("acme")).entries == 1


async def test_each_tenant_has_its_own_chain(log: InMemoryAuditLog) -> None:
    await _appended(log, 2, "acme")
    await _appended(log, 3, "globex")
    assert (await log.verify("acme")).entries == 2
    assert (await log.verify("globex")).entries == 3
    assert (await log.entries("globex"))[0].seq == 1


async def test_a_changed_entry_is_found(log: InMemoryAuditLog) -> None:
    await _appended(log, 3)
    log.tamper_for_test("acme", index=1)
    report = await log.verify("acme")
    assert not report.ok
    assert report.first_bad_seq == 2
    assert report.problem == "entry 2 was changed"


async def test_a_removed_entry_is_found(log: InMemoryAuditLog) -> None:
    await _appended(log, 3)
    entries = await log.entries("acme")
    report = check_chain([entries[0], entries[2]], head_seq=3, head_hash=entries[2].hash)
    assert not report.ok
    assert report.problem == "entry 2 is missing"


async def test_swapped_entries_are_found(log: InMemoryAuditLog) -> None:
    await _appended(log, 3)
    first, second, third = await log.entries("acme")
    swapped = [
        dataclasses.replace(second, seq=1),
        dataclasses.replace(first, seq=2),
        third,
    ]
    report = check_chain(swapped, head_seq=3, head_hash=third.hash)
    assert not report.ok
    assert report.first_bad_seq == 1


async def test_a_cut_off_tail_is_found_by_the_head(log: InMemoryAuditLog) -> None:
    """The remaining chain is self-consistent: only the head record shows entries are missing."""
    await _appended(log, 3)
    entries = await log.entries("acme")
    report = check_chain(entries[:2], head_seq=3, head_hash=entries[2].hash)
    assert not report.ok
    assert report.problem == "the head expects 3 entries"


async def test_an_entry_inserted_at_the_end_is_found_by_the_head(log: InMemoryAuditLog) -> None:
    await _appended(log, 2)
    entries = await log.entries("acme")
    forged = dataclasses.replace(
        entries[1],
        seq=3,
        previous=entries[1].hash,
        hash=chained(entries[1].hash, entries[1].entry),
    )
    report = check_chain([*entries, forged], head_seq=2, head_hash=entries[1].hash)
    assert not report.ok


async def test_a_rewritten_link_is_found(log: InMemoryAuditLog) -> None:
    """The stored link isn't part of any hash, so only comparing it can show it was changed."""
    await _appended(log, 2)
    first, second = await log.entries("acme")
    forged = dataclasses.replace(second, previous="f" * 64)
    report = check_chain([first, forged], head_seq=2, head_hash=second.hash)
    assert not report.ok
    assert report.problem == "entry 2 doesn't follow the one before it"


async def test_a_rewritten_hash_is_found(log: InMemoryAuditLog) -> None:
    await _appended(log, 2)
    first, second = await log.entries("acme")
    forged = dataclasses.replace(first, hash="f" * 64)
    report = check_chain([forged, second], head_seq=2, head_hash=second.hash)
    assert not report.ok
    assert report.first_bad_seq == 1


async def test_entries_can_be_read_in_pages(log: InMemoryAuditLog) -> None:
    await _appended(log, 5)
    page = await log.entries("acme", after=2, limit=2)
    assert [e.seq for e in page] == [3, 4]


# --------------------------------------------------------------------------- the recorder


async def _until_recorded(log: AuditLog, tenant: str, count: int) -> None:
    async with asyncio.timeout(5):
        while (await log.verify(tenant)).entries < count:  # noqa: ASYNC110 - no event to wait on
            await asyncio.sleep(0.01)


async def test_the_recorder_copies_governed_events_only(
    bus: InMemoryEventBus, log: InMemoryAuditLog
) -> None:
    recorder = AuditRecorder(bus, log)
    await recorder.start()
    governed = [
        make_event("policy.decision.denied"),
        make_event("tool.call.completed"),
        make_event("approval.granted"),
        make_event("security.suspended"),
        make_event("app.installed"),
        make_event("run.cancel.requested"),
    ]
    await bus.publish(make_event("step.started"), *governed, make_event("model.call.completed"))
    await _until_recorded(log, "acme", len(governed))
    await recorder.stop()
    recorded = {entry.event.type for entry in await log.entries("acme")}
    assert recorded == {event.type for event in governed}


async def test_a_failing_log_retries_and_loses_nothing(bus: InMemoryEventBus) -> None:
    class Flaky(InMemoryAuditLog):
        failures = 2

        async def append(self, event: object) -> bool:
            if self.failures:
                self.failures -= 1
                raise ConnectionError("database down")
            return await super().append(event)  # type: ignore[arg-type]

    log = Flaky()
    recorder = AuditRecorder(bus, log)
    await recorder.start()
    await bus.publish(make_event("tool.call.completed"))
    await _until_recorded(log, "acme", 1)
    await recorder.stop()
    assert (await log.verify("acme")).ok


async def test_the_recorder_works_across_tenants(
    bus: InMemoryEventBus, log: InMemoryAuditLog
) -> None:
    recorder = AuditRecorder(bus, log)
    await recorder.start()
    await bus.publish(make_event("tool.call.completed", tenant="globex"))
    await _until_recorded(log, "globex", 1)
    await recorder.stop()
    assert (await log.verify("acme")).entries == 0
    assert [s async for s in bus.replay(Scope("globex"))]
