"""Memory OS: what agents and administrators call (§7.7; FR-MEM-02, 03, 05, 10, 13).

* **Scopes bind to the caller, not to arguments.** An agent never names an owner. "user" means the
  user this run acts for, and "session" means this run's conversation, so a prompt can't talk an
  agent into reading somebody else's memory.
* **Every read and write is decided per scope** (FR-MEM-05). The Security Kernel's default rule
  stops an agent whose context holds untrusted content from writing to anything shared, which is
  how memory poisoning spreads.
* **Retrieved content is untrusted and keeps its label.** The caller's context takes on both, so a
  later tool call is escalated, and a ``restricted`` item can't reach a public model through a
  prompt (FR-MEM-13, FR-SEC-11).
* **Embeddings are optional.** Without a usable model, items are stored and found by keyword, and
  ``restricted`` text simply isn't embedded by a public model. Search says which mode it used.
* **Ephemeral items stay in this process.** They never reach the database, as §5.5 requires.
* **Nothing written to the log quotes an item or a query,** so ``forget`` really erases.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from aios.core.catalog import (
    MemoryDocumentSynced,
    MemoryForgotten,
    MemoryIngestStarted,
    MemoryRecalled,
    MemoryWritten,
    PolicyDecided,
    Usage,
)
from aios.core.envelope import DataClass, Event
from aios.core.errors import MemoryConflictError, ModelError, NotFoundError, PolicyDeniedError
from aios.core.identity import Identity
from aios.core.ids import new_ulid, ulid_for, ulid_time
from aios.core.memory import (
    GLOBAL_OWNER,
    GLOBAL_TENANT,
    ContextPack,
    Erasure,
    MemoryHit,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    Provenance,
    Retention,
    SearchMode,
    SearchResult,
    Written,
    memory_filters,
)
from aios.core.policy import (
    Actor,
    AllowAll,
    Effect,
    PolicyAction,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
    Resource,
)
from aios.core.ports import CallSite, EmbeddingRequest, MemoryGateway, ModelGateway
from aios.core.replay import holding_back
from aios.fabric.port import EventBus, Scope
from aios.memory.rank import assemble, chunked, fuse
from aios.memory.store import Area, InMemoryMemoryStore, MemoryStore, Stored, Vector

logger = logging.getLogger("aios.memory")

CANDIDATES = 50
"""How many items each search method offers to ranking, however few the caller wants."""
MIN_SIMILARITY = 0.25
"""Vector search always has a nearest neighbour, however unrelated. Below this cosine similarity
it isn't a match, and offering it would spend the budget and taint the agent for nothing. A first
estimate: the embedding-model eval of ADR-009 should calibrate it."""
EMBEDDING_BATCH = 64
SESSION_TTL = timedelta(days=7)
ORIGIN = "memory-"
MEMORY_AGENT = "memory-os"
SOURCE = "aios://{tenant}/system/memory"
INGESTIBLE_SCOPES = ("app", "tenant", "global")
DOCUMENT_SUFFIXES = (".md", ".markdown", ".txt", ".rst")


def default_retention(scope: MemoryScope) -> Retention:
    return {"working": "ephemeral", "session": "ttl"}.get(scope, "persistent")  # type: ignore[return-value]


def subject_hash(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Document:
    uri: str
    text: str


def documents_in(folder: Path) -> tuple[str, list[Document]]:
    """A folder's text documents, as a source prefix and its documents.

    URIs start at the folder's own name, so ``kb/password-reset.md`` means the same on any machine.
    """
    root = folder.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{folder} is not a folder")
    documents = [
        Document(f"{root.name}/{path.relative_to(root).as_posix()}", path.read_text("utf-8"))
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in DOCUMENT_SUFFIXES
    ]
    return f"{root.name}/", documents


@dataclass(frozen=True, slots=True)
class SyncReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0
    embedded: bool = False


class MemoryService(MemoryGateway):
    def __init__(
        self,
        bus: EventBus,
        store: MemoryStore,
        *,
        policy: PolicyDecisionPoint | None = None,
        models: ModelGateway | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self.policy = policy or AllowAll()
        self.models = models
        self.ephemeral = InMemoryMemoryStore()

    # ------------------------------------------------------------------ agents: writing

    async def put(
        self,
        content: str,
        *,
        scope: MemoryScope,
        site: CallSite,
        subject: str | None = None,
        kind: MemoryKind = "semantic",
        retention: Retention | None = None,
        ttl: timedelta | None = None,
        sensitivity: DataClass | None = None,
        importance: float = 0.5,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> Written:
        await self._authorize(PolicyAction.MEMORY_WRITE, scope, site)
        area = self._area(scope, site)
        if area is None or area.owner is None:
            raise PolicyDeniedError(f"{site.agent} has no {scope} memory: nobody owns it here")
        kept: Retention = retention or default_retention(scope)
        if kept == "legal_hold":
            raise PolicyDeniedError("a legal hold is set by an administrator, not by an agent")
        now = datetime.now(UTC)
        label = DataClass.strictest(
            site.data_class, site.event.dataclass, sensitivity or site.data_class
        )
        item = MemoryItem(
            id=self._item_id(site),
            tenant=site.event.tenantid,
            scope=scope,
            owner=area.owner,
            content=content,
            subject=subject if subject is not None else (area.owner if scope == "user" else None),
            kind=kind,
            retention=kept,
            expires_at=now + (ttl or SESSION_TTL) if kept == "ttl" else None,
            sensitivity=label,
            importance=importance,
            metadata=dict(metadata or {}),
            provenance=Provenance(
                author=site.principal, source_event=site.event.id, created_at=now
            ),
            updated_at=now,
        )
        if await self._wrote(site):  # storing it again could undo an erasure made since
            return Written(item=item)
        if holding_back(site.event) is not None:  # a replay or fork stores nothing
            await self._written(site, "memory.item.written", item, stubbed=True)
            return Written(item=item)
        vector, usage = await self._embed_one(content, label, site)
        await self._holder(item).add([Stored(self._embedded(item, vector), vector)])
        await self._written(site, "memory.item.written", item)
        return Written(item=item, usage=usage)

    async def _wrote(self, site: CallSite) -> bool:
        """Whether an earlier execution of this task already made this write."""
        event = site.event
        if event.runid is None:
            return False
        key = site.key("memory")
        scope = Scope(event.tenantid, event.appid)
        async for stored in self.bus.replay(scope, "memory.item.*", run_id=event.runid):
            data = stored.event.data
            if (
                site.recorded_by(stored.event)
                and isinstance(data, dict)
                and data.get("site") == key
            ):
                return True
        return False

    @staticmethod
    def _item_id(site: CallSite) -> str:
        """The same however many times a task runs, so nothing is stored twice."""
        if site.event.runid is None:
            return new_ulid()
        return ulid_for(f"{site.task}/{site.key('memory')}", ulid_time(site.task))

    @staticmethod
    def _embedded(item: MemoryItem, vector: Vector | None) -> MemoryItem:
        if vector is None:
            return item.model_copy(update={"embedding_model": None, "dimensions": None})
        update = {"embedding_model": vector.model, "dimensions": vector.dimensions}
        return item.model_copy(update=update)

    def _holder(self, item: MemoryItem) -> MemoryStore:
        return self.ephemeral if item.retention == "ephemeral" else self.store

    async def update(
        self,
        item_id: str,
        *,
        site: CallSite,
        content: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        importance: float | None = None,
        version: int | None = None,
    ) -> Written:
        item = await self._visible(item_id, site)
        if item is None:
            raise MemoryConflictError(f"no item {item_id} here")
        await self._authorize(PolicyAction.MEMORY_WRITE, item.scope, site)
        if await self._wrote(site):
            return Written(item=item)
        if item.retention == "legal_hold":
            raise PolicyDeniedError(f"item {item_id} is under a legal hold")
        if version is not None and version != item.version:
            raise MemoryConflictError(f"item {item_id} is at version {item.version}, not {version}")
        text = content if content is not None else item.content
        if holding_back(site.event) is not None:  # a replay or fork changes nothing
            unchanged = item.model_copy(update={"content": text, "version": item.version + 1})
            await self._written(site, "memory.item.updated", unchanged, stubbed=True)
            return Written(item=unchanged)
        vector, usage = await self._embed_one(text, item.sensitivity, site)
        changed = self._embedded(item, vector).model_copy(
            update={
                "content": text,
                "metadata": dict(metadata) if metadata is not None else item.metadata,
                "importance": importance if importance is not None else item.importance,
                "version": item.version + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._holder(item).replace(changed, vector, version=item.version)
        await self._written(site, "memory.item.updated", changed)
        return Written(item=changed, usage=usage)

    async def forget(
        self, *, site: CallSite, item: str | None = None, subject: str | None = None
    ) -> Erasure:
        """An agent can erase what it can see, and the user it acts for can ask to be forgotten."""
        tenant = site.event.tenantid
        if (item is None) == (subject is None):
            raise ValueError("forget needs exactly one of item or subject")
        if item is not None:
            found = await self._visible(item, site)
            if found is None:
                return Erasure()
            await self._authorize(PolicyAction.MEMORY_WRITE, found.scope, site)
        elif subject != site.event.onbehalfof:
            raise PolicyDeniedError("an agent can only erase the user it acts for")
        if holding_back(site.event) is not None:  # a replay or fork erases nothing
            return Erasure()
        return await self._erase(tenant, site.event.appid, site.principal, item, subject)

    # ------------------------------------------------------------------ agents: reading

    async def search(
        self,
        query: str,
        *,
        scopes: Sequence[MemoryScope],
        site: CallSite,
        k: int = 5,
        filters: Mapping[str, Any] | None = None,
        mode: SearchMode = "hybrid",
    ) -> SearchResult:
        areas, policies = await self._readable(scopes, site)
        result = await self._search(
            site, areas, query, k=k, filters=memory_filters(dict(filters or {})), mode=mode
        )
        items = [hit.item for hit in result.hits]
        await self._read(
            site, "search", scopes=scopes, items=items, mode=result.mode, policies=policies
        )
        return result

    async def recall(
        self, query: str, *, scopes: Sequence[MemoryScope], budget_tokens: int, site: CallSite
    ) -> ContextPack:
        areas, policies = await self._readable(scopes, site)
        found = await self._search(site, areas, query, k=CANDIDATES, filters={}, mode="hybrid")
        pack = assemble(found.hits, budget_tokens=budget_tokens, mode=found.mode)
        chosen = {citation.id for citation in pack.citations}
        items = [hit.item for hit in found.hits if hit.item.id in chosen]
        await self._read(
            site, "recall", scopes=scopes, items=items, mode=found.mode, policies=policies
        )
        return pack.model_copy(update={"usage": found.usage})

    async def get(self, item_id: str, *, site: CallSite) -> MemoryItem | None:
        item = await self._visible(item_id, site)
        if item is None:
            return None
        decision = await self._authorize(PolicyAction.MEMORY_READ, item.scope, site)
        await self._read(
            site,
            "get",
            scopes=[item.scope],
            items=[item],
            mode="keyword",
            policies=list(decision.policies),
        )
        return item

    async def _visible(self, item_id: str, site: CallSite) -> MemoryItem | None:
        """The item, if it belongs to this caller. Anything else looks as if it didn't exist."""
        tenant = site.event.tenantid
        item = await self._fetch(tenant, item_id)
        if item is None:
            return None
        area = self._area(item.scope, site)
        return item if area is not None and area.owner == item.owner else None

    async def _fetch(self, tenant: str, item_id: str) -> MemoryItem | None:
        kept = await self.ephemeral.get(tenant, item_id)
        item = kept if kept is not None else await self.store.get(tenant, item_id)
        return item if item is not None and item.live(datetime.now(UTC)) else None

    async def _readable(
        self, scopes: Sequence[MemoryScope], site: CallSite
    ) -> tuple[list[Area], list[str]]:
        areas: list[Area] = []
        policies: list[str] = []
        for scope in dict.fromkeys(scopes):
            decision = await self._authorize(PolicyAction.MEMORY_READ, scope, site)
            policies += decision.policies
            area = self._area(scope, site)
            if area is not None:
                areas.append(area)
        return areas, policies

    @staticmethod
    def _area(scope: MemoryScope, site: CallSite) -> Area | None:
        """The part of ``scope`` that belongs to this call, or ``None`` if nothing does."""
        event = site.event
        owners: dict[MemoryScope, str | None] = {
            "working": event.runid,
            "session": event.correlationid or event.runid,
            "user": event.onbehalfof,
            "agent": site.agent,
            "app": event.appid,
            "tenant": event.tenantid,
            "global": GLOBAL_OWNER,
        }
        owner = owners[scope]
        return None if owner is None else Area(scope, owner)

    async def _search(
        self,
        site: CallSite,
        areas: Sequence[Area],
        query: str,
        *,
        k: int,
        filters: Mapping[str, Any],
        mode: SearchMode,
    ) -> SearchResult:
        tenant = site.event.tenantid
        now = datetime.now(UTC)
        stores = (self.store, self.ephemeral)
        present = [
            store for store in stores if areas and await store.any_in(tenant, areas, now=now)
        ]
        if not present:  # nothing to find, so no embedding to pay for
            return SearchResult(mode="keyword" if mode == "hybrid" else mode)
        vector, usage = None, Usage()
        if mode != "keyword":
            vector, usage = await self._embed_one(query, site.event.dataclass, site)
        keyword: list[MemoryHit] = []
        nearest: list[MemoryHit] = []
        for store in present:
            if mode != "vector":
                keyword += await store.keyword(
                    tenant, areas, query, limit=CANDIDATES, filters=filters, now=now
                )
            if vector is not None:
                found = await store.nearest(
                    tenant, areas, vector, limit=CANDIDATES, filters=filters, now=now
                )
                nearest += [hit for hit in found if hit.score >= MIN_SIMILARITY]
        used: SearchMode = "keyword" if vector is None and mode != "vector" else mode
        hits = fuse(_ranked(keyword), _ranked(nearest), now=now)[:k]
        return SearchResult(hits=hits, mode=used, usage=usage)

    # ------------------------------------------------------------------ embeddings

    async def _embed_one(
        self, text: str, label: DataClass, site: CallSite
    ) -> tuple[Vector | None, Usage]:
        vectors, usage = await self._embed([text], label, site)
        return (vectors[0] if vectors else None), usage

    async def _embed(
        self, texts: list[str], label: DataClass, site: CallSite
    ) -> tuple[list[Vector], Usage]:
        """Vectors for ``texts``, or none when no permitted model can make them.

        The call gets its own site: a task re-executed after a crash must not mistake it for a
        model call of its own, and it counts neither as one nor against that call's position.
        """
        if self.models is None:
            return [], Usage()
        request = EmbeddingRequest(texts=texts, data_class=label)
        embedding = replace(site, origin=ORIGIN)
        if not await self.models.can_embed(request, embedding):
            return [], Usage()
        try:
            response = await self.models.embed(request, embedding)
        except ModelError as exc:
            logger.warning("no embeddings, falling back to keywords: %s", exc)
            return [], Usage()
        model = f"{response.provider}/{response.model}"
        return [Vector(model, tuple(values)) for values in response.vectors], response.usage

    # ------------------------------------------------------------------ administration

    async def search_as(
        self,
        by: Identity,
        query: str,
        *,
        scopes: Sequence[MemoryScope],
        k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> list[MemoryHit]:
        """A tenant admin's view: every owner in these scopes, by keyword."""
        now = datetime.now(UTC)
        areas = [Area(scope) for scope in dict.fromkeys(scopes)]
        wanted = memory_filters(dict(filters or {}))
        hits: list[MemoryHit] = []
        for store in (self.store, self.ephemeral):
            hits += await store.keyword(
                by.tenant, areas, query, limit=CANDIDATES, filters=wanted, now=now
            )
        chosen = fuse(_ranked(hits), [], now=now)[:k]
        if chosen:
            payload = MemoryRecalled(
                operation="search",
                scopes=list(scopes),
                items=[hit.item.id for hit in chosen],
            )
            await self._tenant_event(by, "memory.recalled", payload.model_dump(mode="json"))
        return chosen

    async def get_as(self, by: Identity, item_id: str) -> MemoryItem | None:
        item = await self._fetch(by.tenant, item_id)
        if item is not None:
            payload = MemoryRecalled(operation="get", scopes=[item.scope], items=[item.id])
            await self._tenant_event(by, "memory.recalled", payload.model_dump(mode="json"))
        return item

    async def update_as(
        self, by: Identity, item_id: str, *, content: str, version: int, global_too: bool = False
    ) -> MemoryItem:
        """A person corrects an item (FR-UI-07): a new version, with the change on the record.

        The new text has no vector until something writes the item again, so until then it is
        found by keyword. Global memory, which every tenant reads, needs ``global_too``.
        """
        item = await self._fetch(by.tenant, item_id)
        if item is None:
            raise NotFoundError(f"no memory item {item_id}")
        if item.tenant == GLOBAL_TENANT and not global_too:
            raise PolicyDeniedError("global memory is the operator's to change")
        if item.retention == "legal_hold":
            raise PolicyDeniedError(f"item {item_id} is under a legal hold")
        if version != item.version:
            raise MemoryConflictError(f"item {item_id} is at version {item.version}, not {version}")
        changed = self._embedded(item, None).model_copy(
            update={
                "content": content,
                "version": item.version + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._holder(item).replace(changed, None, version=item.version)
        payload = MemoryWritten(
            item=changed.id,
            scope=changed.scope,
            kind=changed.kind,
            retention=changed.retention,
            sensitivity=changed.sensitivity.value,
            version=changed.version,
        )
        await self._tenant_event(by, "memory.item.updated", payload.model_dump(mode="json"))
        return changed

    async def remember_as(
        self,
        by: Identity,
        content: str,
        *,
        event: Event,
        kind: MemoryKind = "episodic",
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> MemoryItem:
        """What a person asks an App to remember (FR-HUM-09): its memory, written by them, from
        ``event``. Whoever calls this has checked they may. Until something writes it again it
        has no vector, so it is found by keyword."""
        now = datetime.now(UTC)
        item = MemoryItem(
            id=ulid_for(f"{event.id}/remembered", event.time),
            tenant=event.tenantid,
            scope="app",
            owner=event.appid,
            content=content,
            kind=kind,
            retention=default_retention("app"),
            sensitivity=DataClass.strictest(DataClass.INTERNAL, event.dataclass),
            metadata=dict(metadata or {}),
            provenance=Provenance(author=by.principal, source_event=event.id, created_at=now),
            updated_at=now,
        )
        if await self._fetch(item.tenant, item.id) is not None:
            return item  # remembered already
        await self.store.add([Stored(self._embedded(item, None), None)])
        payload = MemoryWritten(
            item=item.id,
            scope=item.scope,
            kind=item.kind,
            retention=item.retention,
            sensitivity=item.sensitivity.value,
            version=item.version,
        )
        await self._tenant_event(
            by, "memory.item.written", payload.model_dump(mode="json"), app=event.appid
        )
        return item

    async def forget_as(
        self, by: Identity, *, item: str | None = None, subject: str | None = None
    ) -> Erasure:
        if (item is None) == (subject is None):
            raise ValueError("forget needs exactly one of item or subject")
        return await self._erase(by.tenant, _PLATFORM_APP, by.principal, item, subject)

    async def _erase(
        self, tenant: str, app: str, principal: str, item: str | None, subject: str | None
    ) -> Erasure:
        now = datetime.now(UTC)
        erased, held = await self.store.erase(tenant, item=item, subject=subject, now=now)
        gone, kept = await self.ephemeral.erase(tenant, item=item, subject=subject, now=now)
        ids = erased + gone
        payload = MemoryForgotten(
            items=ids,
            held=held + kept,
            subject_hash=subject_hash(subject) if subject is not None else None,
        )
        await self.bus.publish(
            Event(
                source=SOURCE.format(tenant=tenant),
                type="memory.item.forgotten",
                tenantid=tenant,
                appid=app,
                principal=principal,
                data=payload.model_dump(mode="json"),
            )
        )
        return Erasure(erased=len(ids), held=held + kept)

    async def erase_app(self, by: Identity, app: str) -> Erasure:
        """Erase what an App holds as its own (FR-APP-05). People's own memories aren't its."""
        ids, held = await self.store.erase_area(by.tenant, Area("app", app))
        payload = MemoryForgotten(items=ids, held=held)
        await self.bus.publish(
            Event(
                source=SOURCE.format(tenant=by.tenant),
                type="memory.item.forgotten",
                tenantid=by.tenant,
                appid=app,
                principal=by.principal,
                data=payload.model_dump(mode="json"),
            )
        )
        return Erasure(erased=len(ids), held=held)

    async def of_app(self, tenant: str, app: str) -> list[MemoryItem]:
        return await self.store.in_area(tenant, Area("app", app))

    async def sync(
        self,
        by: Identity,
        app: str,
        documents: Sequence[Document],
        *,
        source: str,
        scope: MemoryScope = "app",
        sensitivity: DataClass = DataClass.INTERNAL,
        prune: bool = True,
    ) -> SyncReport:
        """Bring a set of documents into memory (FR-MEM-07).

        Unchanged documents cost nothing. Changed ones swap their chunks at once, so a search
        never sees half a document. With ``prune``, ``documents`` is the whole of ``source``, and
        whatever is stored under it but no longer listed is erased.
        """
        if scope not in INGESTIBLE_SCOPES:
            raise ValueError(f"documents can't be ingested into {scope} memory")
        tenant = GLOBAL_TENANT if scope == "global" else by.tenant
        owner = {"app": app, "tenant": by.tenant, "global": GLOBAL_OWNER}[scope]
        area = Area(scope, owner)
        for document in documents:
            if not document.uri.startswith(source):
                raise ValueError(f"{document.uri} is not under {source}")
        stored = await self.store.documents(tenant, area, source)
        started = await self._tenant_event(
            by,
            "memory.ingest.started",
            MemoryIngestStarted(source=source, documents=len(documents), scope=scope).model_dump(
                mode="json"
            ),
            app=app,
        )
        site = CallSite(
            event=started,
            agent=MEMORY_AGENT,
            principal=f"agent:{MEMORY_AGENT}@{by.tenant}",
            index=0,
            data_class=sensitivity,
        )
        added = updated = unchanged = chunks = 0
        embedded = True
        for document in documents:
            digest = _content_hash(document.text)
            if stored.get(document.uri) == digest:
                unchanged += 1
                continue
            items, had_vectors = await self._chunks(
                document,
                digest=digest,
                tenant=tenant,
                area=area,
                sensitivity=sensitivity,
                by=by,
                site=site,
            )
            site = replace(site, index=site.index + 1)
            await self.store.replace_document(tenant, area, document.uri, items)
            chunks += len(items)
            embedded = embedded and had_vectors
            if document.uri in stored:
                updated += 1
            else:
                added += 1
        listed = {document.uri for document in documents}
        gone = [uri for uri in stored if uri not in listed] if prune else []
        for uri in gone:
            await self.store.replace_document(tenant, area, uri, [])
        report = SyncReport(added, updated, unchanged, len(gone), chunks, embedded and chunks > 0)
        await self.bus.publish(
            started.derive(
                "memory.document.synced",
                MemoryDocumentSynced(
                    source=source,
                    scope=scope,
                    added=added,
                    updated=updated,
                    unchanged=unchanged,
                    removed=len(gone),
                    chunks=chunks,
                    embedded=report.embedded,
                ).model_dump(mode="json"),
                source=SOURCE.format(tenant=by.tenant),
                principal=by.principal,
            )
        )
        return report

    async def _chunks(
        self,
        document: Document,
        *,
        digest: str,
        tenant: str,
        area: Area,
        sensitivity: DataClass,
        by: Identity,
        site: CallSite,
    ) -> tuple[list[Stored], bool]:
        pieces = chunked(document.text)
        vectors: list[Vector] = []
        for start in range(0, len(pieces), EMBEDDING_BATCH):
            batch = [piece.text for piece in pieces[start : start + EMBEDDING_BATCH]]
            found, _ = await self._embed(
                batch, sensitivity, replace(site, item=start // EMBEDDING_BATCH)
            )
            vectors += found
        complete = len(vectors) == len(pieces)
        now = datetime.now(UTC)
        items: list[Stored] = []
        for index, piece in enumerate(pieces):
            metadata: dict[str, JsonValue] = {"heading": piece.heading} if piece.heading else {}
            vector = vectors[index] if complete else None
            item = MemoryItem(
                id=new_ulid(),
                tenant=tenant,
                scope=area.scope,
                owner=cast(str, area.owner),
                content=piece.text,
                sensitivity=sensitivity,
                metadata=metadata,
                provenance=Provenance(
                    author=by.principal,
                    source_event=site.event.id,
                    source_uri=document.uri,
                    created_at=now,
                ),
                document=document.uri,
                chunk=piece.index,
                document_hash=digest,
                updated_at=now,
            )
            items.append(Stored(self._embedded(item, vector), vector))
        return items, complete

    async def expire(self) -> int:
        """Delete what has expired. Reads already skip it; this frees the space."""
        now = datetime.now(UTC)
        return await self.store.expire(now) + await self.ephemeral.expire(now)

    # ------------------------------------------------------------------ policy and events

    async def _authorize(
        self, action: PolicyAction, scope: MemoryScope, site: CallSite
    ) -> PolicyDecision:
        delegate = site.event.onbehalfof
        decision = await self.policy.decide(
            PolicyRequest(
                tenant=site.event.tenantid,
                app=site.event.appid,
                actor=Actor(site.principal),
                action=action,
                resource=Resource.memory_scope(scope),
                on_behalf_of=Actor(delegate) if delegate is not None else None,
                tainted=site.tainted,
            )
        )
        if decision.effect is Effect.ALLOW:
            return decision
        payload = PolicyDecided(
            action=action.value,
            resource=scope,
            policies=list(decision.policies),
            approvers=list(decision.approvers),
            reason=decision.reason,
        )
        await self._site_event(site, "policy.decision.denied", payload.model_dump(mode="json"))
        reason = decision.reason or "denied by policy"
        raise PolicyDeniedError(f"{site.agent}: {action.value} on {scope} memory: {reason}")

    async def _site_event(
        self,
        site: CallSite,
        type: str,
        data: dict[str, Any],
        label: DataClass = DataClass.PUBLIC,
        *,
        tainted: bool = False,
    ) -> None:
        await self.bus.publish(
            site.event.derive(
                type,
                data,
                source=SOURCE.format(tenant=site.event.tenantid),
                principal=site.principal,
                dataclass=DataClass.strictest(label, site.data_class),
                tainted=site.tainted or tainted,
            )
        )

    async def _written(
        self, site: CallSite, type: str, item: MemoryItem, *, stubbed: bool = False
    ) -> None:
        payload = MemoryWritten(
            stubbed=stubbed,
            item=item.id,
            scope=item.scope,
            kind=item.kind,
            retention=item.retention,
            sensitivity=item.sensitivity.value,
            version=item.version,
            site=site.key("memory"),
        )
        await self._site_event(site, type, payload.model_dump(mode="json"), item.sensitivity)

    async def _read(
        self,
        site: CallSite,
        operation: str,
        *,
        scopes: Sequence[MemoryScope],
        items: Sequence[MemoryItem],
        mode: SearchMode,
        policies: list[str],
    ) -> None:
        """Data access is on the record once data moved (FR-SEC-08). Finding nothing isn't."""
        if not items:
            return
        payload = MemoryRecalled.model_validate(
            {
                "operation": operation,
                "scopes": list(scopes),
                "items": [item.id for item in items],
                "mode": mode,
                "policies": list(dict.fromkeys(policies)),
            }
        )
        label = DataClass.strictest(*(item.sensitivity for item in items))
        data = payload.model_dump(mode="json")
        await self._site_event(site, "memory.recalled", data, label, tainted=True)

    async def _tenant_event(
        self, by: Identity, type: str, data: dict[str, Any], *, app: str | None = None
    ) -> Event:
        event = Event(
            source=SOURCE.format(tenant=by.tenant),
            type=type,
            tenantid=by.tenant,
            appid=app or _PLATFORM_APP,
            principal=by.principal,
            data=data,
        )
        await self.bus.publish(event)
        return event


_PLATFORM_APP = "aios"


def _ranked(hits: Sequence[MemoryHit]) -> list[MemoryHit]:
    return sorted(hits, key=lambda hit: (-hit.score, hit.item.id))
