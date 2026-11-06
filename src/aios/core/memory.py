"""What Memory OS stores and returns (requirements §5.5, §7.7; FR-MEM-01, 03, 04).

Two independent dimensions describe an item: its **scope** says who can see it and its **retention**
says how long it lives. Every item records where it came from and how sensitive it is.

Content lives only in the memory store. Events describe what happened to an item without quoting
it, because the event log is append-only and ``forget`` has to be able to erase the content.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from aios.core.catalog import Usage
from aios.core.envelope import DataClass, Principal
from aios.core.ids import Ulid

_WRAPPER = re.compile(r"</?memory\b[^>]*>")

MemoryScope = Literal["working", "session", "user", "agent", "app", "tenant", "global"]
Retention = Literal["ephemeral", "ttl", "persistent", "legal_hold"]
MemoryKind = Literal["episodic", "semantic", "procedural"]
SearchMode = Literal["hybrid", "keyword", "vector"]

GLOBAL_TENANT = "_global"
"""Where the global scope lives. It can't be a real tenant: tenant ids are DNS labels."""
GLOBAL_OWNER = "global"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Provenance(_Frozen):
    """Where an item came from (FR-MEM-04)."""

    author: Principal
    source_event: Ulid | None = None
    source_uri: str | None = None
    created_at: AwareDatetime


class MemoryItem(_Frozen):
    id: Ulid
    tenant: str
    scope: MemoryScope
    owner: str = Field(description="Who the scope belongs to: a run, a user, an agent or an App.")
    content: str
    subject: Principal | None = Field(
        default=None, description="Whom the item is about; forget(subject=...) erases by this."
    )
    kind: MemoryKind = "semantic"
    retention: Retention = "persistent"
    expires_at: AwareDatetime | None = None
    sensitivity: DataClass = DataClass.INTERNAL
    importance: float = Field(default=0.5, ge=0, le=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    provenance: Provenance
    document: str | None = Field(
        default=None, description="The ingested document this chunk belongs to."
    )
    chunk: int | None = None
    document_hash: str | None = None
    embedding_model: str | None = Field(
        default=None, description="Vectors are only compared within one model (ADR-009)."
    )
    dimensions: int | None = None
    version: int = Field(default=1, ge=1)
    updated_at: AwareDatetime

    def live(self, now: datetime) -> bool:
        return self.retention == "legal_hold" or self.expires_at is None or self.expires_at > now


class MemoryHit(_Frozen):
    item: MemoryItem
    score: float = 0.0


class Citation(_Frozen):
    id: Ulid
    scope: MemoryScope
    source_uri: str | None = None
    score: float = 0.0


class ContextPack(_Frozen):
    """What ``recall`` hands an agent: cited, deduplicated and fitted to a token budget.

    Its text is untrusted (FR-MEM-13). Formatting a pack into a prompt gives that text, wrapped so
    that a model can tell retrieved data from instructions.
    """

    text: str = ""
    citations: list[Citation] = Field(default_factory=list[Citation])
    tokens: int = 0
    sensitivity: DataClass = DataClass.PUBLIC
    mode: SearchMode = "keyword"
    usage: Usage = Field(default_factory=Usage)

    def __str__(self) -> str:
        return self.text

    def __bool__(self) -> bool:
        return bool(self.text)

    @property
    def content(self) -> str:
        """The retrieved text alone. Whatever in it posed as a wrapper was escaped already."""
        return _WRAPPER.sub("", self.text)


class SearchResult(_Frozen):
    hits: list[MemoryHit] = Field(default_factory=list[MemoryHit])
    mode: SearchMode = "keyword"
    usage: Usage = Field(default_factory=Usage)


class Written(_Frozen):
    """An item as stored, and what embedding it cost."""

    item: MemoryItem
    usage: Usage = Field(default_factory=Usage)


class Erasure(_Frozen):
    """What ``forget`` did. Items under legal hold stay, and are counted (FR-MEM-10)."""

    erased: int = 0
    held: int = 0


def memory_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Filters with no ``None`` values: a missing filter and an empty one mean the same."""
    return {key: value for key, value in (filters or {}).items() if value is not None}
