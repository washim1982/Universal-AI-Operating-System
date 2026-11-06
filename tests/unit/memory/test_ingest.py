"""Document ingestion (FR-MEM-07): chunks, provenance, re-sync and deletion propagation."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aios.core.envelope import DataClass
from aios.core.identity import Identity
from aios.core.ports import CallSite
from aios.fabric import InMemoryEventBus, Scope
from aios.memory import Document, InMemoryMemoryStore, MemoryService, documents_in
from aios.memory.service import EMBEDDING_BATCH
from aios.memory.store import Area, Vector
from aios.testing import task_request
from aios.testing.memory import Embedder, direction

ROOT = Path(__file__).resolve().parents[3]
TENANT, APP = "acme", "desk"
BY = Identity("user:ops@acme", TENANT, frozenset({"tenant-admin"}))
AREA = Area("app", APP)


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus(validate_payloads=True) as bus:
        yield bus


@pytest.fixture
def models() -> Embedder:
    return Embedder()


@pytest.fixture
def memory(bus: InMemoryEventBus, models: Embedder) -> MemoryService:
    return MemoryService(bus, InMemoryMemoryStore(), models=models)


def doc(name: str, text: str = "Refunds take five days.") -> Document:
    return Document(f"kb/{name}", text)


async def _chunks(memory: MemoryService, query: str = "refunds") -> list[str]:
    hits = await memory.store.keyword(TENANT, [AREA], query, limit=50, filters={}, now=_now())
    return [hit.item.content for hit in hits]


def _now() -> datetime:
    return datetime.now(UTC)


async def test_a_document_becomes_searchable_chunks(memory: MemoryService) -> None:
    report = await memory.sync(BY, APP, [doc("a.md")], source="kb/")
    assert (report.added, report.chunks, report.embedded) == (1, 1, True)
    assert await _chunks(memory) == ["Refunds take five days."]


async def test_every_chunk_records_its_source(memory: MemoryService) -> None:
    await memory.sync(BY, APP, [doc("a.md")], source="kb/")
    [hit] = await memory.store.keyword(TENANT, [AREA], "refunds", limit=5, filters={}, now=_now())
    item = hit.item
    assert item.provenance.source_uri == "kb/a.md"
    assert item.provenance.author == BY.principal
    assert item.provenance.source_event is not None
    assert (item.document, item.chunk) == ("kb/a.md", 0)
    assert item.embedding_model == "test/embedder-1"


async def test_an_unchanged_document_costs_nothing(memory: MemoryService, models: Embedder) -> None:
    await memory.sync(BY, APP, [doc("a.md")], source="kb/")
    models.requests.clear()
    report = await memory.sync(BY, APP, [doc("a.md")], source="kb/")
    assert (report.added, report.updated, report.unchanged) == (0, 0, 1)
    assert models.requests == []


async def test_a_changed_document_replaces_all_its_chunks(memory: MemoryService) -> None:
    await memory.sync(BY, APP, [doc("a.md", "Refunds take five days.")], source="kb/")
    report = await memory.sync(BY, APP, [doc("a.md", "Refunds take ten days.")], source="kb/")
    assert report.updated == 1
    assert await _chunks(memory) == ["Refunds take ten days."]


async def test_a_document_that_has_gone_is_erased(memory: MemoryService) -> None:
    """Deletion propagation: a retired article must stop answering questions."""
    await memory.sync(BY, APP, [doc("a.md"), doc("b.md", "Refunds are free.")], source="kb/")
    report = await memory.sync(BY, APP, [doc("a.md")], source="kb/")
    assert report.removed == 1
    assert await _chunks(memory) == ["Refunds take five days."]


async def test_without_prune_other_documents_stay(memory: MemoryService) -> None:
    await memory.sync(BY, APP, [doc("a.md")], source="kb/")
    report = await memory.sync(
        BY, APP, [doc("b.md", "Refunds are free.")], source="kb/", prune=False
    )
    assert report.removed == 0
    assert len(await _chunks(memory)) == 2


async def test_a_sync_only_touches_its_own_source(memory: MemoryService) -> None:
    await memory.sync(BY, APP, [Document("other/a.md", "Refunds elsewhere.")], source="other/")
    await memory.sync(BY, APP, [doc("a.md")], source="kb/")
    await memory.sync(BY, APP, [], source="kb/")
    assert await _chunks(memory) == ["Refunds elsewhere."]


async def test_a_document_outside_its_source_is_refused(memory: MemoryService) -> None:
    with pytest.raises(ValueError, match="not under"):
        await memory.sync(BY, APP, [Document("other/a.md", "x")], source="kb/")


@pytest.mark.parametrize("scope", ["working", "session", "user", "agent"])
async def test_documents_go_only_into_shared_scopes(memory: MemoryService, scope: str) -> None:
    with pytest.raises(ValueError, match="can't be ingested"):
        await memory.sync(BY, APP, [doc("a.md")], source="kb/", scope=scope)  # type: ignore[arg-type]


async def test_a_long_document_is_embedded_in_batches(
    memory: MemoryService, models: Embedder
) -> None:
    text = "\n\n".join(f"Paragraph {index} says something. " * 12 for index in range(200))
    report = await memory.sync(BY, APP, [doc("a.md", text)], source="kb/")
    assert report.chunks > EMBEDDING_BATCH
    assert len(models.requests) > 1
    assert all(len(request.texts) <= EMBEDDING_BATCH for request, _ in models.requests)
    keys = [site.key("model") for _, site in models.requests]
    assert len(set(keys)) == len(keys)


async def test_a_failing_model_still_leaves_documents_searchable(
    memory: MemoryService, models: Embedder
) -> None:
    models.fail = True
    report = await memory.sync(BY, APP, [doc("a.md")], source="kb/")
    assert report.chunks == 1
    assert report.embedded is False
    assert await _chunks(memory) == ["Refunds take five days."]


async def test_a_partly_failed_embedding_stores_no_vectors_at_all(
    memory: MemoryService, models: Embedder
) -> None:
    """Half a document in vector search would answer from whichever half happened to embed."""
    calls = 0
    real = models.embed

    async def fail_later(request: object, site: CallSite) -> object:
        nonlocal calls
        calls += 1
        models.fail = calls > 1
        return await real(request, site)  # type: ignore[arg-type]

    models.embed = fail_later  # type: ignore[assignment, method-assign]
    text = "\n\n".join(f"Paragraph {index} says something. " * 12 for index in range(200))
    report = await memory.sync(BY, APP, [doc("a.md", text)], source="kb/")
    assert calls > 1
    assert report.embedded is False
    stored = await memory.store.keyword(
        TENANT, [AREA], "paragraph", limit=50, filters={}, now=_now()
    )
    assert stored
    assert {hit.item.embedding_model for hit in stored} == {None}
    assert (
        await memory.store.nearest(
            TENANT,
            [AREA],
            Vector("test/embedder-1", tuple(direction("paragraph"))),
            limit=50,
            filters={},
            now=_now(),
        )
        == []
    )


async def test_a_sensitive_document_keeps_its_label(
    memory: MemoryService, models: Embedder
) -> None:
    await memory.sync(BY, APP, [doc("a.md")], source="kb/", sensitivity=DataClass.CONFIDENTIAL)
    [(request, _)] = models.requests
    assert request.data_class == DataClass.CONFIDENTIAL
    [hit] = await memory.store.keyword(TENANT, [AREA], "refunds", limit=5, filters={}, now=_now())
    assert hit.item.sensitivity == DataClass.CONFIDENTIAL


async def test_a_sync_leaves_a_record_without_the_text(
    memory: MemoryService, bus: InMemoryEventBus
) -> None:
    await memory.sync(BY, APP, [doc("a.md", "private policy words")], source="kb/")
    events = [s.event async for s in bus.replay(Scope(TENANT))]
    assert [e.type for e in events] == ["memory.ingest.started", "memory.document.synced"]
    assert events[1].causationid == events[0].id
    assert events[1].principal == BY.principal
    assert "private policy words" not in " ".join(e.model_dump_json() for e in events)


async def test_tenant_scope_is_shared_by_all_apps(memory: MemoryService) -> None:
    await memory.sync(BY, APP, [doc("a.md")], source="kb/", scope="tenant")
    hits = await memory.store.keyword(
        TENANT, [Area("tenant", TENANT)], "refunds", limit=5, filters={}, now=_now()
    )
    assert len(hits) == 1


async def test_global_documents_reach_every_tenant(memory: MemoryService) -> None:
    await memory.sync(
        BY, APP, [doc("law.md", "Refunds are a legal right.")], source="kb/", scope="global"
    )
    site = CallSite(
        event=task_request("x", tenantid="globex", appid=APP),
        agent="a",
        principal="agent:a@globex",
        index=0,
    )
    result = await memory.search("refunds", scopes=["global"], site=site)
    assert [hit.item.content for hit in result.hits] == ["Refunds are a legal right."]


def test_a_folder_gives_stable_uris() -> None:
    source, documents = documents_in(ROOT / "tests" / "fixtures" / "kb")
    assert source == "kb/"
    assert [d.uri for d in documents] == ["kb/password-reset.md", "kb/returns-policy.md"]


def test_only_text_documents_are_read(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("markdown", "utf-8")
    (tmp_path / "b.txt").write_text("text", "utf-8")
    (tmp_path / "c.exe").write_bytes(b"\x00binary")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "d.md").write_text("nested", "utf-8")
    _, documents = documents_in(tmp_path)
    assert sorted(d.uri.split("/", 1)[1] for d in documents) == ["a.md", "b.txt", "nested/d.md"]


def test_a_missing_folder_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        documents_in(tmp_path / "nope")
