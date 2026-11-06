"""pgvector and PostgreSQL full-text search behind the memory store (ADR-009; FR-MEM-06, 11).

One table holds items, their search text and their vectors, so erasure is a single transaction and
no second copy can be left behind.

* **Isolation.** Searches run on a tenant connection, so row-level security applies whatever a
  query says. Global items sit under a tenant id no real tenant can have, and only they are shared.
* **Vectors.** The column takes any size, because the embedding model is configuration. HNSW needs
  one size per index, so each size gets its own partial index the first time it is stored. Sizes
  above pgvector's index limit are searched exactly. Iterative scanning keeps a tenant's results
  complete when other tenants' vectors dominate the neighbourhood.
* **Keywords.** A question is matched on any of its words, not all of them: agents ask whole
  sentences, and ranking takes care of the rest.

Writes use the connecting role, because a tenant connection can only read.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from aios.core.errors import MemoryConflictError
from aios.core.memory import GLOBAL_TENANT, MemoryHit, MemoryItem, Provenance
from aios.fabric.postgres import TENANT_ROLE, PostgresEventBus
from aios.memory.store import Area, MemoryStore, Stored, Vector

MAX_INDEXED_DIMENSIONS = 2000
_TABLE = "memory_items"
_LIVE = "(retention = 'legal_hold' OR expires_at IS NULL OR expires_at > $now)"

_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS {s}.memory_items (
    tenantid        text NOT NULL,
    id              text NOT NULL,
    scope           text NOT NULL,
    owner           text NOT NULL,
    content         text NOT NULL,
    subject         text,
    kind            text NOT NULL,
    retention       text NOT NULL,
    expires_at      timestamptz,
    sensitivity     text NOT NULL,
    importance      double precision NOT NULL,
    metadata        jsonb NOT NULL,
    author          text NOT NULL,
    source_event    text,
    source_uri      text,
    document        text,
    chunk           integer,
    document_hash   text,
    embedding       vector,
    embedding_model text,
    dimensions      integer,
    version         integer NOT NULL,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    search          tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    PRIMARY KEY (tenantid, id)
);
CREATE INDEX IF NOT EXISTS memory_area ON {s}.memory_items (tenantid, scope, owner);
CREATE INDEX IF NOT EXISTS memory_subject ON {s}.memory_items (tenantid, subject)
    WHERE subject IS NOT NULL;
CREATE INDEX IF NOT EXISTS memory_document ON {s}.memory_items (tenantid, document)
    WHERE document IS NOT NULL;
CREATE INDEX IF NOT EXISTS memory_expiry ON {s}.memory_items (expires_at)
    WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS memory_search ON {s}.memory_items USING gin (search);
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_policies
        WHERE schemaname = '{s}' AND tablename = 'memory_items'
          AND policyname = 'tenant_isolation'
    ) THEN
        ALTER TABLE {s}.memory_items ENABLE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {s}.memory_items TO aios_tenant
            USING (tenantid IN (current_setting('aios.tenant', true), '_global'));
    END IF;
END $$;
"""

_COLUMNS = (
    "tenantid, id, scope, owner, content, subject, kind, retention, expires_at, sensitivity, "
    "importance, metadata, author, source_event, source_uri, document, chunk, document_hash, "
    "embedding_model, dimensions, version, created_at, updated_at"
)
_INSERT = f"""
INSERT INTO {{s}}.{_TABLE} ({_COLUMNS}, embedding)
SELECT {_COLUMNS}, embedding::vector FROM unnest(
    $1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[], $7::text[], $8::text[],
    $9::timestamptz[], $10::text[], $11::double precision[], $12::jsonb[], $13::text[], $14::text[],
    $15::text[], $16::text[], $17::integer[], $18::text[], $19::text[], $20::integer[],
    $21::integer[], $22::timestamptz[], $23::timestamptz[], $24::text[]
) AS t({_COLUMNS}, embedding)
ON CONFLICT (tenantid, id) DO NOTHING
"""  # noqa: S608 - only constants are interpolated


def _item(row: Any) -> MemoryItem:
    return MemoryItem(
        id=row["id"],
        tenant=row["tenantid"],
        scope=row["scope"],
        owner=row["owner"],
        content=row["content"],
        subject=row["subject"],
        kind=row["kind"],
        retention=row["retention"],
        expires_at=row["expires_at"],
        sensitivity=row["sensitivity"],
        importance=row["importance"],
        metadata=row["metadata"],
        provenance=Provenance(
            author=row["author"],
            source_event=row["source_event"],
            source_uri=row["source_uri"],
            created_at=row["created_at"],
        ),
        document=row["document"],
        chunk=row["chunk"],
        document_hash=row["document_hash"],
        embedding_model=row["embedding_model"],
        dimensions=row["dimensions"],
        version=row["version"],
        updated_at=row["updated_at"],
    )


def _literal(vector: Vector) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector.values) + "]"


def _row(stored: Stored) -> tuple[Any, ...]:
    item, vector = stored.item, stored.vector
    return (
        item.tenant,
        item.id,
        item.scope,
        item.owner,
        item.content,
        item.subject,
        item.kind,
        item.retention,
        item.expires_at,
        item.sensitivity.value,
        item.importance,
        item.metadata,
        item.provenance.author,
        item.provenance.source_event,
        item.provenance.source_uri,
        item.document,
        item.chunk,
        item.document_hash,
        vector.model if vector is not None else None,
        vector.dimensions if vector is not None else None,
        item.version,
        item.provenance.created_at,
        item.updated_at,
        _literal(vector) if vector is not None else None,
    )


class _Where:
    """Conditions shared by every search: whose, which areas, what's alive, and the filters.

    Naming the tenant here repeats what row-level security enforces: each covers for a mistake in
    the other.
    """

    def __init__(
        self, tenant: str, areas: Sequence[Area], filters: Mapping[str, Any], now: datetime
    ) -> None:
        self.args: list[Any] = [now, [tenant, GLOBAL_TENANT]]
        self.parts = [_LIVE.replace("$now", "$1"), "tenantid = ANY($2::text[])"]
        self._areas(areas)
        self._filters(filters)

    def arg(self, value: Any) -> str:
        self.args.append(value)
        return f"${len(self.args)}"

    def _areas(self, areas: Sequence[Area]) -> None:
        wanted = []
        for area in areas:
            scope = self.arg(area.scope)
            owned = "" if area.owner is None else f" AND owner = {self.arg(area.owner)}"
            wanted.append(f"(scope = {scope}{owned})")
        self.parts.append("(" + " OR ".join(wanted) + ")" if wanted else "FALSE")

    def _filters(self, filters: Mapping[str, Any]) -> None:
        metadata = {k: v for k, v in filters.items() if k not in ("kind", "subject")}
        for column in ("kind", "subject"):
            if column in filters:
                self.parts.append(f"{column} = {self.arg(filters[column])}")
        if metadata:
            self.parts.append(f"metadata @> {self.arg(metadata)}::jsonb")

    def __str__(self) -> str:
        return " AND ".join(self.parts)


class PostgresMemoryStore(MemoryStore):
    def __init__(self, bus: PostgresEventBus) -> None:
        self.bus = bus
        self._ready = False
        self._lock = asyncio.Lock()
        self._indexed: set[int] = set()

    async def _setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            grant = f"GRANT SELECT ON {self.bus.schema}.{_TABLE} TO {TENANT_ROLE};"
            await self.bus.set_up("memory", self.bus.sql(_DDL) + grant)
            self._ready = True

    async def _index(self, dimensions: int) -> None:
        """An HNSW index for vectors of this size, once.

        Only a missing index is created: even when it exists, ``CREATE INDEX`` waits for writers.
        """
        if dimensions in self._indexed or dimensions > MAX_INDEXED_DIMENSIONS:
            return
        name = f"memory_hnsw_{dimensions}"
        statement = (
            f"SELECT pg_advisory_xact_lock(hashtext('{{s}}.{name}'));"
            f"CREATE INDEX IF NOT EXISTS {name} ON {{s}}.{_TABLE} USING hnsw "
            f"((embedding::vector({dimensions})) vector_cosine_ops) "
            f"WHERE dimensions = {int(dimensions)}"
        )
        async with self.bus.pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", f"{self.bus.schema}.{name}"
            )
            if not exists:
                await connection.execute(self.bus.sql(statement))
        self._indexed.add(dimensions)

    async def _insert(self, connection: Any, items: Sequence[Stored]) -> None:
        if items:
            rows = [_row(stored) for stored in items]
            columns = [list(column) for column in zip(*rows, strict=True)]
            await connection.execute(self.bus.sql(_INSERT), *columns)

    async def _index_vectors(self, items: Sequence[Stored]) -> None:
        for size in {s.vector.dimensions for s in items if s.vector is not None}:
            await self._index(size)

    async def add(self, items: Sequence[Stored]) -> None:
        await self._setup()
        await self._index_vectors(items)
        async with self.bus.pool.acquire() as connection:
            await self._insert(connection, items)

    async def get(self, tenant: str, item_id: str) -> MemoryItem | None:
        await self._setup()
        live = _LIVE.replace("$now", "clock_timestamp()")
        query = self.bus.sql(
            "SELECT * FROM {s}.memory_items "  # noqa: S608 - only a constant is interpolated
            f"WHERE id = $1 AND tenantid = ANY($2::text[]) AND {live}"
        )
        async with self.bus.tenant_connection(tenant) as connection:
            row = await connection.fetchrow(query, item_id, [tenant, GLOBAL_TENANT])
        return None if row is None else _item(row)

    async def replace(self, item: MemoryItem, vector: Vector | None, *, version: int) -> None:
        await self._setup()
        if vector is not None:
            await self._index(vector.dimensions)
        status = await self._write(
            "UPDATE {s}.memory_items SET content = $3, metadata = $4::jsonb, importance = $5, "
            "version = $6, updated_at = $7, embedding = $8::text::vector, embedding_model = $9, "
            "dimensions = $10 WHERE tenantid = $1 AND id = $2 AND version = $11",
            item.tenant,
            item.id,
            item.content,
            item.metadata,
            item.importance,
            item.version,
            item.updated_at,
            _literal(vector) if vector is not None else None,
            vector.model if vector is not None else None,
            vector.dimensions if vector is not None else None,
            version,
        )
        if status != "UPDATE 1":
            raise MemoryConflictError(f"item {item.id} is not at version {version}")

    async def _write(self, query: str, *args: Any) -> str:
        async with self.bus.pool.acquire() as connection:
            return await connection.execute(self.bus.sql(query), *args)

    async def erase(
        self,
        tenant: str,
        *,
        item: str | None = None,
        subject: str | None = None,
        now: datetime,
    ) -> tuple[list[str], int]:
        await self._setup()
        if item is not None:
            chosen, args = "id = $2", [tenant, item]
        else:
            chosen = "(subject = $2 OR (scope = 'user' AND owner = $2))"
            args = [tenant, subject]
        where = f"tenantid = $1 AND {chosen}"
        sql = self.bus.sql
        async with self.bus.pool.acquire() as connection, connection.transaction():
            held = await connection.fetchval(
                sql(f"SELECT count(*) FROM {{s}}.memory_items WHERE {where} "  # noqa: S608
                    "AND retention = 'legal_hold'"),
                *args,
            )  # fmt: skip
            rows = await connection.fetch(
                sql(f"DELETE FROM {{s}}.memory_items WHERE {where} "  # noqa: S608
                    "AND retention <> 'legal_hold' RETURNING id"),
                *args,
            )  # fmt: skip
        return [row["id"] for row in rows], held

    async def erase_area(self, tenant: str, area: Area) -> tuple[list[str], int]:
        await self._setup()
        where = "tenantid = $1 AND scope = $2 AND owner = $3"
        args = (tenant, area.scope, area.owner)
        sql = self.bus.sql
        async with self.bus.pool.acquire() as connection, connection.transaction():
            held = await connection.fetchval(
                sql(f"SELECT count(*) FROM {{s}}.memory_items WHERE {where} "  # noqa: S608
                    "AND retention = 'legal_hold'"),
                *args,
            )  # fmt: skip
            rows = await connection.fetch(
                sql(f"DELETE FROM {{s}}.memory_items WHERE {where} "  # noqa: S608
                    "AND retention <> 'legal_hold' RETURNING id"),
                *args,
            )  # fmt: skip
        return [row["id"] for row in rows], int(held)

    async def in_area(self, tenant: str, area: Area) -> list[MemoryItem]:
        await self._setup()
        query = self.bus.sql(
            "SELECT * FROM {s}.memory_items WHERE scope = $1 AND owner = $2 ORDER BY id"
        )
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(query, area.scope, area.owner)
        return [_item(row) for row in rows]

    async def any_in(self, tenant: str, areas: Sequence[Area], *, now: datetime) -> bool:
        await self._setup()
        where = _Where(tenant, areas, {}, now)
        query = self.bus.sql(
            f"SELECT EXISTS (SELECT 1 FROM {{s}}.memory_items WHERE {where})"  # noqa: S608
        )
        async with self.bus.tenant_connection(tenant) as connection:
            return await connection.fetchval(query, *where.args)

    async def keyword(
        self,
        tenant: str,
        areas: Sequence[Area],
        query: str,
        *,
        limit: int,
        filters: Mapping[str, Any],
        now: datetime,
    ) -> list[MemoryHit]:
        await self._setup()
        where = _Where(tenant, areas, filters, now)
        wanted = where.arg(query)
        cap = where.arg(limit)
        statement = self.bus.sql(
            "WITH words AS (SELECT string_agg(quote_literal(lexeme), ' | ')::tsquery AS query "  # noqa: S608
            f"FROM unnest(to_tsvector('english', {wanted}))) "
            "SELECT i.*, ts_rank_cd(i.search, words.query, 32) AS rank "
            f"FROM {{s}}.memory_items i, words WHERE i.search @@ words.query AND {where} "
            f"ORDER BY rank DESC, id LIMIT {cap}"
        )
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(statement, *where.args)
        return [MemoryHit(item=_item(row), score=float(row["rank"])) for row in rows]

    async def nearest(
        self,
        tenant: str,
        areas: Sequence[Area],
        vector: Vector,
        *,
        limit: int,
        filters: Mapping[str, Any],
        now: datetime,
    ) -> list[MemoryHit]:
        await self._setup()
        where = _Where(tenant, areas, filters, now)
        size = int(vector.dimensions)
        model = where.arg(vector.model)
        wanted = where.arg(_literal(vector))
        cap = where.arg(limit)
        distance = f"embedding::vector({size}) <=> {wanted}::text::vector({size})"
        statement = self.bus.sql(
            f"SELECT *, 1 - ({distance}) AS similarity FROM {{s}}.memory_items "  # noqa: S608
            f"WHERE dimensions = {size} AND embedding_model = {model} AND {where} "
            f"ORDER BY {distance}, id LIMIT {cap}"
        )
        async with self.bus.tenant_connection(tenant) as connection:
            await connection.execute(
                "SELECT set_config('hnsw.iterative_scan', 'strict_order', true)"
            )
            rows = await connection.fetch(statement, *where.args)
        return [MemoryHit(item=_item(row), score=float(row["similarity"])) for row in rows]

    async def documents(self, tenant: str, area: Area, source: str) -> dict[str, str]:
        await self._setup()
        statement = self.bus.sql(
            "SELECT DISTINCT document, document_hash FROM {s}.memory_items WHERE tenantid = $1 "
            "AND scope = $2 AND owner = $3 AND starts_with(document, $4)"
        )
        async with self.bus.tenant_connection(tenant) as connection:
            rows = await connection.fetch(statement, tenant, area.scope, area.owner, source)
        return {row["document"]: row["document_hash"] or "" for row in rows}

    async def replace_document(
        self, tenant: str, area: Area, uri: str, items: Sequence[Stored]
    ) -> None:
        await self._setup()
        await self._index_vectors(items)
        async with self.bus.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                self.bus.sql(
                    "DELETE FROM {s}.memory_items WHERE tenantid = $1 AND scope = $2 "
                    "AND owner = $3 AND document = $4"
                ),
                tenant,
                area.scope,
                area.owner,
                uri,
            )
            await self._insert(connection, items)

    async def expire(self, now: datetime) -> int:
        await self._setup()
        status = await self._write(
            "DELETE FROM {s}.memory_items WHERE expires_at <= $1 AND retention <> 'legal_hold'",
            now,
        )
        return int(status.split()[-1])
