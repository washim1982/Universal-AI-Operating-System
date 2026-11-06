"""The memory store contract (FR-MEM-02, 11): one suite for every backend.

An adapter passes by subclassing :class:`MemoryStoreConformance` and giving it a ``store`` fixture.
pgvector runs it now, and Qdrant will run the same suite in Phase 4.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aios.core.catalog import Usage
from aios.core.envelope import DataClass
from aios.core.errors import MemoryConflictError, ModelError
from aios.core.ids import new_ulid
from aios.core.memory import GLOBAL_OWNER, GLOBAL_TENANT, MemoryItem, Provenance
from aios.core.ports import (
    CallSite,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from aios.memory.store import Area, MemoryStore, Stored, Vector

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
APP = Area("app", "desk")
ALL: dict[str, Any] = {"limit": 10, "filters": {}, "now": NOW}
MODEL = "test/embedder"


def direction(text: str, size: int = 32) -> list[float]:
    """A stand-in embedding: each word points one way, so shared words mean similar vectors."""
    values = [0.0] * size
    for word in text.lower().split():
        digest = hashlib.sha256(word.strip(".,?!").encode()).digest()
        values[digest[0] % size] += 1.0
    length = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / length for v in values]


class Embedder(ModelGateway):
    """A model gateway that only embeds, and records what it was asked."""

    def __init__(self, *, private_only: bool = False) -> None:
        self.requests: list[tuple[EmbeddingRequest, CallSite]] = []
        self.private_only = private_only
        self.fail = False
        self.available = True
        self.model = "embedder-1"

    async def generate(self, request: ModelRequest, site: CallSite) -> ModelResponse:
        raise NotImplementedError

    async def can_embed(self, request: EmbeddingRequest, site: CallSite) -> bool:
        if self.private_only and request.data_class == DataClass.RESTRICTED:
            return False
        return self.available

    async def embed(self, request: EmbeddingRequest, site: CallSite) -> EmbeddingResponse:
        self.requests.append((request, site))
        if self.fail:
            raise ModelError("the provider is down")
        return EmbeddingResponse(
            vectors=[direction(text) for text in request.texts],
            provider="test",
            model=self.model,
            usage=Usage(input_tokens=4, cost_usd=0.001),
        )


def item(content: str = "refund policy", *, tenant: str = "acme", **fields: Any) -> MemoryItem:
    values: dict[str, Any] = {
        "id": new_ulid(),
        "tenant": tenant,
        "scope": "app",
        "owner": "desk",
        "content": content,
        "provenance": Provenance(author="system:test", created_at=NOW),
        "updated_at": NOW,
        **fields,
    }
    return MemoryItem(**values)


def vector(*values: float, model: str = MODEL) -> Vector:
    return Vector(model, values)


def _only(limit: int) -> dict[str, Any]:
    return {**ALL, "limit": limit}


async def _ids(store: MemoryStore, query: str, *areas: Area, tenant: str = "acme") -> set[str]:
    hits = await store.keyword(tenant, areas or (APP,), query, **ALL)
    return {hit.item.id for hit in hits}


class MemoryStoreConformance:
    # ------------------------------------------------------------------ add and get

    async def test_a_stored_item_comes_back_whole(self, store: MemoryStore) -> None:
        stored = item(
            subject="user:alice@acme",
            kind="procedural",
            sensitivity="confidential",
            importance=0.8,
            metadata={"ticket": "4812", "nested": {"a": [1, 2]}},
        )
        await store.add([Stored(stored)])
        assert await store.get("acme", stored.id) == stored

    async def test_an_unknown_item_is_none(self, store: MemoryStore) -> None:
        assert await store.get("acme", new_ulid()) is None

    async def test_storing_an_id_again_keeps_the_first(self, store: MemoryStore) -> None:
        """A task re-executed after a crash writes the same item again."""
        first = item("first")
        await store.add([Stored(first)])
        await store.add([Stored(first.model_copy(update={"content": "second"}))])
        found = await store.get("acme", first.id)
        assert found is not None
        assert found.content == "first"

    async def test_an_expired_item_is_gone_at_once(self, store: MemoryStore) -> None:
        stale = item(retention="ttl", expires_at=datetime.now(UTC) - timedelta(seconds=1))
        await store.add([Stored(stale)])
        assert await store.get("acme", stale.id) is None

    # ------------------------------------------------------------------ tenants and areas

    async def test_another_tenants_item_can_not_be_read(self, store: MemoryStore) -> None:
        theirs = item("order 1003 for acme")
        await store.add([Stored(theirs, vector(1.0, 0.0))])
        assert await store.get("globex", theirs.id) is None
        assert await _ids(store, "order", tenant="globex") == set()
        assert await store.nearest("globex", [APP], vector(1.0, 0.0), **ALL) == []
        assert not await store.any_in("globex", [APP], now=NOW)

    async def test_two_tenants_can_use_one_id(self, store: MemoryStore) -> None:
        shared = new_ulid()
        await store.add([Stored(item("acme's", id=shared))])
        await store.add([Stored(item("globex's", id=shared, tenant="globex"))])
        found = await store.get("globex", shared)
        assert found is not None
        assert found.content == "globex's"

    async def test_global_items_reach_every_tenant(self, store: MemoryStore) -> None:
        shared = item(
            "public refund law",
            tenant=GLOBAL_TENANT,
            scope="global",
            owner=GLOBAL_OWNER,
        )
        area = Area("global", GLOBAL_OWNER)
        await store.add([Stored(shared, vector(1.0, 0.0))])
        for tenant in ("acme", "globex"):
            found = await store.get(tenant, shared.id)
            assert found is not None
            assert found.content == "public refund law"
            assert await _ids(store, "refund", area, tenant=tenant) == {shared.id}
            near = await store.nearest(tenant, [area], vector(1.0, 0.0), **ALL)
            assert [hit.item.id for hit in near] == [shared.id]

    async def test_a_search_stays_in_its_areas(self, store: MemoryStore) -> None:
        mine = item("refund note", scope="user", owner="user:alice@acme")
        other = item("refund note", scope="user", owner="user:bob@acme")
        app = item("refund note")
        await store.add([Stored(mine), Stored(other), Stored(app)])
        assert await _ids(store, "refund", Area("user", "user:alice@acme")) == {mine.id}
        assert await _ids(store, "refund", Area("user", "user:bob@acme")) == {other.id}
        assert await _ids(store, "refund", APP) == {app.id}
        assert await _ids(store, "refund", Area("session", "s")) == set()

    async def test_an_area_without_an_owner_means_every_owner(self, store: MemoryStore) -> None:
        one = item("refund note", scope="user", owner="user:alice@acme")
        two = item("refund note", scope="user", owner="user:bob@acme")
        await store.add([Stored(one), Stored(two)])
        assert await _ids(store, "refund", Area("user")) == {one.id, two.id}

    async def test_no_areas_means_nothing(self, store: MemoryStore) -> None:
        await store.add([Stored(item("refund note"), vector(1.0, 0.0))])
        assert await store.keyword("acme", [], "refund", **ALL) == []
        assert await store.nearest("acme", [], vector(1.0, 0.0), **ALL) == []
        assert not await store.any_in("acme", [], now=NOW)

    async def test_any_in_ignores_expired_items(self, store: MemoryStore) -> None:
        await store.add([Stored(item(retention="ttl", expires_at=NOW - timedelta(hours=1)))])
        assert not await store.any_in("acme", [APP], now=NOW)
        await store.add([Stored(item())])
        assert await store.any_in("acme", [APP], now=NOW)

    # ------------------------------------------------------------------ keyword search

    async def test_a_question_finds_items_with_any_of_its_words(self, store: MemoryStore) -> None:
        """Agents ask whole sentences, and an item rarely holds every word."""
        hit = item("Reset your password from the account page.")
        await store.add([Stored(hit), Stored(item("Opening hours are nine to five."))])
        assert await _ids(store, "How do I reset my password today?") == {hit.id}

    async def test_word_forms_match(self, store: MemoryStore) -> None:
        hit = item("Refunds are issued within five days.")
        await store.add([Stored(hit)])
        assert await _ids(store, "refund") == {hit.id}

    async def test_more_matching_words_rank_higher(self, store: MemoryStore) -> None:
        strong = item("refund policy for broken items")
        weak = item("broken window at the office")
        await store.add([Stored(weak), Stored(strong)])
        hits = await store.keyword("acme", [APP], "refund policy broken", **ALL)
        assert [hit.item.id for hit in hits] == [strong.id, weak.id]

    @pytest.mark.parametrize("query", ["the and of", "", "   ", "!!!", "???"])
    async def test_a_query_with_no_words_finds_nothing(
        self, store: MemoryStore, query: str
    ) -> None:
        await store.add([Stored(item())])
        assert await store.keyword("acme", [APP], query, **ALL) == []

    @pytest.mark.parametrize(
        "query",
        [
            "refund'; DROP TABLE memory_items; --",
            "refund' OR '1'='1",
            "refund & | ! ( ) : *",
            "(((refund",
            'say "refund" now',
            "refund\\",
            "refund\x00policy".replace("\x00", " "),
            "ÿ退款 refund",
        ],
    )
    async def test_a_query_is_only_ever_data(self, store: MemoryStore, query: str) -> None:
        hit = item("refund policy")
        await store.add([Stored(hit)])
        assert await _ids(store, query) == {hit.id}

    async def test_expired_items_are_not_found(self, store: MemoryStore) -> None:
        await store.add([Stored(item(retention="ttl", expires_at=NOW - timedelta(hours=1)))])
        assert await _ids(store, "refund") == set()

    async def test_filters_narrow_a_keyword_search(self, store: MemoryStore) -> None:
        semantic = item(metadata={"source": "ticket-1"})
        procedural = item(kind="procedural", subject="user:alice@acme")
        await store.add([Stored(semantic), Stored(procedural)])

        async def found(**filters: Any) -> set[str]:
            options: dict[str, Any] = {**ALL, "filters": filters}
            hits = await store.keyword("acme", [APP], "refund", **options)
            return {hit.item.id for hit in hits}

        assert await found(kind="procedural") == {procedural.id}
        assert await found(subject="user:alice@acme") == {procedural.id}
        assert await found(source="ticket-1") == {semantic.id}
        assert await found(source="ticket-2") == set()

    async def test_a_search_stops_at_its_limit(self, store: MemoryStore) -> None:
        await store.add([Stored(item()) for _ in range(5)])
        options: dict[str, Any] = {**ALL, "limit": 2}
        assert len(await store.keyword("acme", [APP], "refund", **options)) == 2

    # ------------------------------------------------------------------ vector search

    async def test_nearest_vectors_come_first(self, store: MemoryStore) -> None:
        near = item("near")
        middle = item("middle")
        far = item("far")
        await store.add(
            [
                Stored(far, vector(-1.0, 0.0)),
                Stored(near, vector(1.0, 0.05)),
                Stored(middle, vector(0.5, 0.9)),
            ]
        )
        hits = await store.nearest("acme", [APP], vector(1.0, 0.0), **ALL)
        assert [hit.item.id for hit in hits] == [near.id, middle.id, far.id]
        assert hits[0].score > hits[1].score > hits[2].score
        assert hits[0].score == pytest.approx(0.9988, abs=0.001)

    async def test_vectors_of_another_model_are_never_compared(self, store: MemoryStore) -> None:
        """Their numbers mean nothing to each other (ADR-009)."""
        mine = item("mine")
        await store.add(
            [
                Stored(mine, vector(1.0, 0.0)),
                Stored(item("other"), vector(1.0, 0.0, model="test/other")),
            ]
        )
        hits = await store.nearest("acme", [APP], vector(1.0, 0.0), **ALL)
        assert [hit.item.id for hit in hits] == [mine.id]

    async def test_vectors_of_another_size_are_never_compared(self, store: MemoryStore) -> None:
        small = item("small")
        large = item("large")
        await store.add([Stored(small, vector(1.0, 0.0)), Stored(large, vector(1.0, 0.0, 0.0))])
        for query, expected in (
            (vector(1.0, 0.0), small),
            (vector(1.0, 0.0, 0.0), large),
        ):
            hits = await store.nearest("acme", [APP], query, **ALL)
            assert [hit.item.id for hit in hits] == [expected.id]

    async def test_an_item_with_no_vector_is_only_found_by_keyword(
        self, store: MemoryStore
    ) -> None:
        plain = item("refund policy")
        await store.add([Stored(plain)])
        assert await store.nearest("acme", [APP], vector(1.0, 0.0), **ALL) == []
        assert await _ids(store, "refund") == {plain.id}

    async def test_vector_search_honours_expiry_and_filters(self, store: MemoryStore) -> None:
        live = item("live", kind="procedural")
        expired = item("expired", retention="ttl", expires_at=NOW - timedelta(hours=1))
        semantic = item("semantic")
        await store.add(
            [
                Stored(live, vector(1.0, 0.0)),
                Stored(expired, vector(1.0, 0.0)),
                Stored(semantic, vector(1.0, 0.0)),
            ]
        )
        options: dict[str, Any] = {**ALL, "filters": {"kind": "procedural"}}
        hits = await store.nearest("acme", [APP], vector(1.0, 0.0), **options)
        assert [hit.item.id for hit in hits] == [live.id]

    async def test_one_tenants_vectors_do_not_crowd_out_anothers(self, store: MemoryStore) -> None:
        """An index walks the nearest vectors overall: globex's few must still be found."""
        await store.add(
            [Stored(item(f"acme {index}"), vector(1.0, index / 1000)) for index in range(300)]
        )
        theirs = item("globex's only item", tenant="globex")
        await store.add([Stored(theirs, vector(-1.0, 0.0))])
        hits = await store.nearest("globex", [APP], vector(1.0, 0.0), **ALL)
        assert [hit.item.id for hit in hits] == [theirs.id]

    # ------------------------------------------------------------------ versions

    async def test_a_new_version_replaces_the_item(self, store: MemoryStore) -> None:
        first = item("old refund words")
        await store.add([Stored(first, vector(1.0, 0.0))])
        changed = first.model_copy(
            update={
                "content": "new shipping words",
                "version": 2,
                "metadata": {"edited": True},
                "importance": 0.9,
                "updated_at": LATER,
            }
        )
        await store.replace(changed, vector(0.0, 1.0), version=1)
        found = await store.get("acme", first.id)
        assert found is not None
        assert (found.content, found.version) == ("new shipping words", 2)
        assert found.metadata == {"edited": True}
        assert await _ids(store, "refund") == set()
        assert await _ids(store, "shipping") == {first.id}
        hits = await store.nearest("acme", [APP], vector(0.0, 1.0), **_only(1))
        assert hits[0].score == pytest.approx(1.0, abs=0.001)

    async def test_a_stale_version_is_refused(self, store: MemoryStore) -> None:
        first = item()
        await store.add([Stored(first)])
        changed = first.model_copy(update={"content": "edit", "version": 2})
        await store.replace(changed, None, version=1)
        with pytest.raises(MemoryConflictError):
            await store.replace(first.model_copy(update={"version": 2}), None, version=1)

    async def test_a_new_version_can_lose_its_vector(self, store: MemoryStore) -> None:
        first = item()
        await store.add([Stored(first, vector(1.0, 0.0))])
        changed = first.model_copy(update={"version": 2, "embedding_model": None})
        await store.replace(changed, None, version=1)
        assert await store.nearest("acme", [APP], vector(1.0, 0.0), **ALL) == []

    # ------------------------------------------------------------------ erasure

    async def test_erasing_an_item_erases_its_vector(self, store: MemoryStore) -> None:
        gone = item("gone")
        kept = item("kept")
        await store.add([Stored(gone, vector(1.0, 0.0)), Stored(kept, vector(1.0, 0.0))])
        assert await store.erase("acme", item=gone.id, now=NOW) == ([gone.id], 0)
        assert await store.get("acme", gone.id) is None
        hits = await store.nearest("acme", [APP], vector(1.0, 0.0), **ALL)
        assert [hit.item.id for hit in hits] == [kept.id]

    async def test_erasing_a_subject_takes_everything_about_them(self, store: MemoryStore) -> None:
        about = item("alice's ticket", subject="user:alice@acme")
        theirs = item("prefers email", scope="user", owner="user:alice@acme")
        other = item("bob's ticket", subject="user:bob@acme")
        await store.add([Stored(about), Stored(theirs), Stored(other)])
        erased, held = await store.erase("acme", subject="user:alice@acme", now=NOW)
        assert sorted(erased) == sorted([about.id, theirs.id])
        assert held == 0
        assert await store.get("acme", other.id) == other

    async def test_a_legal_hold_survives_erasure_and_is_counted(self, store: MemoryStore) -> None:
        held = item(subject="user:alice@acme", retention="legal_hold")
        await store.add([Stored(held)])
        assert await store.erase("acme", subject="user:alice@acme", now=NOW) == ([], 1)
        assert await store.get("acme", held.id) == held

    async def test_erasure_stays_in_its_tenant(self, store: MemoryStore) -> None:
        theirs = item(subject="user:alice@acme", tenant="globex")
        await store.add([Stored(theirs)])
        assert await store.erase("acme", subject="user:alice@acme", now=NOW) == ([], 0)
        assert await store.erase("acme", item=theirs.id, now=NOW) == ([], 0)
        assert await store.get("globex", theirs.id) == theirs

    # ------------------------------------------------------------------ documents

    @staticmethod
    def _chunk(uri: str, text: str, digest: str = "h1", **fields: Any) -> Stored:
        return Stored(item(text, document=uri, chunk=0, document_hash=digest, **fields))

    async def test_documents_are_listed_under_their_source(self, store: MemoryStore) -> None:
        await store.add(
            [
                self._chunk("kb/a.md", "one", "h1"),
                self._chunk("kb/b.md", "two", "h2"),
                self._chunk("other/c.md", "three", "h3"),
                self._chunk("kb/d.md", "four", "h4", owner="another-app"),
            ]
        )
        assert await store.documents("acme", APP, "kb/") == {"kb/a.md": "h1", "kb/b.md": "h2"}

    async def test_a_document_is_swapped_whole(self, store: MemoryStore) -> None:
        await store.add([self._chunk("kb/a.md", "old refund"), self._chunk("kb/a.md", "old rules")])
        await store.replace_document("acme", APP, "kb/a.md", [self._chunk("kb/a.md", "new policy")])
        assert await _ids(store, "old") == set()
        assert len(await _ids(store, "policy")) == 1

    async def test_a_document_with_no_chunks_is_deleted(self, store: MemoryStore) -> None:
        await store.add([self._chunk("kb/a.md", "refund")])
        await store.replace_document("acme", APP, "kb/a.md", [])
        assert await store.documents("acme", APP, "kb/") == {}
        assert await _ids(store, "refund") == set()

    async def test_replacing_a_document_leaves_other_owners_alone(self, store: MemoryStore) -> None:
        """Two Apps of a tenant can ingest the same path: one's sync can't delete the other's."""
        theirs = self._chunk("kb/a.md", "refund", owner="another-app")
        same_name = self._chunk("kb/a.md", "refund", scope="tenant", owner="desk")  # an App can
        await store.add(
            [self._chunk("kb/a.md", "refund"), theirs, same_name]
        )  # share a tenant's id
        await store.replace_document("acme", APP, "kb/a.md", [])
        assert await _ids(store, "refund", Area("app", "another-app")) == {theirs.item.id}
        assert await _ids(store, "refund", Area("tenant", "desk")) == {same_name.item.id}

    async def test_replacing_a_document_leaves_others_alone(self, store: MemoryStore) -> None:
        keep = self._chunk("kb/b.md", "refund")
        theirs = self._chunk("kb/a.md", "refund", tenant="globex")
        await store.add([self._chunk("kb/a.md", "refund"), keep, theirs])
        await store.replace_document("acme", APP, "kb/a.md", [])
        assert await _ids(store, "refund") == {keep.item.id}
        assert await _ids(store, "refund", tenant="globex") == {theirs.item.id}

    # ------------------------------------------------------------------ expiry

    async def test_expiring_frees_only_what_has_expired(self, store: MemoryStore) -> None:
        past = NOW - timedelta(hours=1)
        expired = item(retention="ttl", expires_at=past)
        live = item(
            retention="ttl", expires_at=datetime.now(UTC) + timedelta(days=1)
        )  # get() reads the clock
        forever = item()
        held = item(retention="legal_hold", expires_at=past)
        other = item(retention="ttl", expires_at=past, tenant="globex")
        await store.add([Stored(i) for i in (expired, live, forever, held, other)])
        assert await store.expire(NOW) == 2
        assert await store.get("acme", live.id) == live
        assert await store.get("acme", forever.id) == forever
        assert await store.get("acme", held.id) == held
