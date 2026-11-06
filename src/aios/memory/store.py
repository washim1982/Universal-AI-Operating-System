"""The storage port (FR-MEM-02, 11) and the in-memory adapter.

A store keeps items and finds candidates. Ranking, policy, embedding and events live above it in
:class:`aios.memory.service.MemoryService`, so every backend behaves alike and only has to pass the
conformance suite in :mod:`aios.testing.memory`.

An :class:`Area` names what a caller may see: a scope, and in it one owner, or any owner for a
tenant admin. A store must never return an item outside the areas it was given, and vectors are
only compared with vectors of the same model and size (ADR-009).
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aios.core.errors import MemoryConflictError
from aios.core.memory import GLOBAL_TENANT, MemoryHit, MemoryItem, MemoryScope

_WORD = re.compile(r"\w+")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does", "for", "from",
        "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "our", "that", "the",
        "their", "this", "to", "was", "we", "what", "when", "where", "which", "who", "why",
        "will", "with", "you", "your",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Area:
    scope: MemoryScope
    owner: str | None = None
    """``None`` means every owner in the scope, for tenant administration."""


@dataclass(frozen=True, slots=True)
class Vector:
    model: str
    values: tuple[float, ...]

    @property
    def dimensions(self) -> int:
        return len(self.values)


@dataclass(frozen=True, slots=True)
class Stored:
    """An item and, when one could be made, its vector."""

    item: MemoryItem
    vector: Vector | None = None


def matches_filters(item: MemoryItem, filters: Mapping[str, Any]) -> bool:
    """``kind`` and ``subject`` compare with the item; other keys compare with its metadata."""
    for key, value in filters.items():
        if key in ("kind", "subject"):
            if getattr(item, key) != value:
                return False
        elif item.metadata.get(key) != value:
            return False
    return True


def _in_areas(item: MemoryItem, areas: Sequence[Area]) -> bool:
    return any(a.scope == item.scope and a.owner in (None, item.owner) for a in areas)


class MemoryStore(ABC):
    @abstractmethod
    async def add(self, items: Sequence[Stored]) -> None:
        """Store items atomically. An id already stored is left as it is (a retried task)."""

    @abstractmethod
    async def get(self, tenant: str, item_id: str) -> MemoryItem | None:
        """An item of this tenant or of the global scope, unless it has expired."""

    @abstractmethod
    async def replace(self, item: MemoryItem, vector: Vector | None, *, version: int) -> None:
        """Store ``item`` as the next version of the one at ``version``; a stale version raises
        :class:`MemoryConflictError`."""

    @abstractmethod
    async def erase(
        self,
        tenant: str,
        *,
        item: str | None = None,
        subject: str | None = None,
        now: datetime,
    ) -> tuple[list[str], int]:
        """Delete items and their vectors in one step; returns their ids and how many a legal
        hold kept. ``subject`` also takes the items in that subject's own user scope."""

    @abstractmethod
    async def erase_area(self, tenant: str, area: Area) -> tuple[list[str], int]:
        """Delete everything in one owner's area, as :meth:`erase` does: ids, and how many held."""

    @abstractmethod
    async def in_area(self, tenant: str, area: Area) -> list[MemoryItem]:
        """Everything in one owner's area, for an export."""

    @abstractmethod
    async def any_in(self, tenant: str, areas: Sequence[Area], *, now: datetime) -> bool: ...

    @abstractmethod
    async def keyword(
        self,
        tenant: str,
        areas: Sequence[Area],
        query: str,
        *,
        limit: int,
        filters: Mapping[str, Any],
        now: datetime,
    ) -> list[MemoryHit]: ...

    @abstractmethod
    async def nearest(
        self,
        tenant: str,
        areas: Sequence[Area],
        vector: Vector,
        *,
        limit: int,
        filters: Mapping[str, Any],
        now: datetime,
    ) -> list[MemoryHit]: ...

    @abstractmethod
    async def documents(self, tenant: str, area: Area, source: str) -> dict[str, str]:
        """The stored documents under the ``source`` prefix: ``{uri: content hash}``."""

    @abstractmethod
    async def replace_document(
        self, tenant: str, area: Area, uri: str, items: Sequence[Stored]
    ) -> None:
        """Swap a document's chunks for ``items`` at once; none of ``items`` deletes it."""

    @abstractmethod
    async def expire(self, now: datetime) -> int:
        """Delete expired items, across tenants. Legal holds never expire."""


class InMemoryMemoryStore(MemoryStore):
    """For ``aios dev`` and tests, and for ephemeral items on every installation."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Stored] = {}

    async def add(self, items: Sequence[Stored]) -> None:
        for stored in items:
            self._items.setdefault((stored.item.tenant, stored.item.id), stored)

    def _visible(self, tenant: str, now: datetime) -> list[Stored]:
        return [
            stored
            for (owner, _), stored in self._items.items()
            if owner in (tenant, GLOBAL_TENANT) and stored.item.live(now)
        ]

    async def get(self, tenant: str, item_id: str) -> MemoryItem | None:
        for owner in (tenant, GLOBAL_TENANT):
            stored = self._items.get((owner, item_id))
            if stored is not None and stored.item.live(datetime.now(UTC)):
                return stored.item
        return None

    async def replace(self, item: MemoryItem, vector: Vector | None, *, version: int) -> None:
        key = (item.tenant, item.id)
        current = self._items.get(key)
        if current is None or current.item.version != version:
            raise MemoryConflictError(f"item {item.id} is not at version {version}")
        self._items[key] = Stored(item, vector)

    async def erase(
        self,
        tenant: str,
        *,
        item: str | None = None,
        subject: str | None = None,
        now: datetime,
    ) -> tuple[list[str], int]:
        chosen = [
            (key, stored)
            for key, stored in self._items.items()
            if key[0] == tenant and _erasing(stored.item, item, subject)
        ]
        erased: list[str] = []
        held = 0
        for key, stored in chosen:
            if stored.item.retention == "legal_hold":
                held += 1
            else:
                del self._items[key]
                erased.append(stored.item.id)
        return erased, held

    async def any_in(self, tenant: str, areas: Sequence[Area], *, now: datetime) -> bool:
        return any(_in_areas(stored.item, areas) for stored in self._visible(tenant, now))

    def _candidates(
        self, tenant: str, areas: Sequence[Area], filters: Mapping[str, Any], now: datetime
    ) -> list[Stored]:
        return [
            stored
            for stored in self._visible(tenant, now)
            if _in_areas(stored.item, areas) and matches_filters(stored.item, filters)
        ]

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
        wanted = _terms(query)
        if not wanted:
            return []
        hits: list[MemoryHit] = []
        for stored in self._candidates(tenant, areas, filters, now):
            terms = Counter(_terms(stored.item.content))
            score = sum(1 + math.log(terms[term]) for term in set(wanted) if terms[term])
            if score:
                hits.append(MemoryHit(item=stored.item, score=score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.item.id))[:limit]

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
        hits = [
            MemoryHit(item=stored.item, score=_cosine(stored.vector.values, vector.values))
            for stored in self._candidates(tenant, areas, filters, now)
            if stored.vector is not None
            and stored.vector.model == vector.model
            and stored.vector.dimensions == vector.dimensions
        ]
        return sorted(hits, key=lambda hit: (-hit.score, hit.item.id))[:limit]

    async def erase_area(self, tenant: str, area: Area) -> tuple[list[str], int]:
        erased: list[str] = []
        held = 0
        for key, stored in list(self._items.items()):
            item = stored.item
            if key[0] != tenant or (item.scope, item.owner) != (area.scope, area.owner):
                continue
            if item.retention == "legal_hold":
                held += 1
            else:
                del self._items[key]
                erased.append(item.id)
        return erased, held

    async def in_area(self, tenant: str, area: Area) -> list[MemoryItem]:
        return [
            stored.item
            for key, stored in self._items.items()
            if key[0] == tenant
            and (stored.item.scope, stored.item.owner) == (area.scope, area.owner)
        ]

    async def documents(self, tenant: str, area: Area, source: str) -> dict[str, str]:
        return {
            stored.item.document: stored.item.document_hash or ""
            for (owner, _), stored in self._items.items()
            if owner == tenant
            and stored.item.document is not None
            and stored.item.document.startswith(source)
            and _in_areas(stored.item, [area])
        }

    async def replace_document(
        self, tenant: str, area: Area, uri: str, items: Sequence[Stored]
    ) -> None:
        for key, stored in list(self._items.items()):
            if key[0] == tenant and stored.item.document == uri and _in_areas(stored.item, [area]):
                del self._items[key]
        await self.add(items)

    async def expire(self, now: datetime) -> int:
        gone = [key for key, stored in self._items.items() if not stored.item.live(now)]
        for key in gone:
            del self._items[key]
        return len(gone)


def _erasing(item: MemoryItem, wanted: str | None, subject: str | None) -> bool:
    if wanted is not None:
        return item.id == wanted
    return item.subject == subject or (item.scope == "user" and item.owner == subject)


def _terms(text: str) -> list[str]:
    return [_stem(word) for word in _WORD.findall(text.lower()) if word not in _STOPWORDS]


def _stem(word: str) -> str:
    """Enough stemming that "refunds" finds "refund", as PostgreSQL's dictionary does."""
    for suffix in ("ing", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)]
    return word


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    product = sum(a * b for a, b in zip(left, right, strict=True))
    size = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return product / size if size else 0.0
