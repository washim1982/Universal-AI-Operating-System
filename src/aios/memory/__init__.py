"""Memory OS: memory API, ranking, ingestion and vector-store adapters (WP 2.1; ADR-009)."""

from aios.fabric.port import EventBus
from aios.fabric.postgres import PostgresEventBus
from aios.memory.service import (
    Document,
    MemoryService,
    SyncReport,
    default_retention,
    documents_in,
    subject_hash,
)
from aios.memory.store import Area, InMemoryMemoryStore, MemoryStore, Stored, Vector

__all__ = [
    "Area",
    "Document",
    "InMemoryMemoryStore",
    "MemoryService",
    "MemoryStore",
    "Stored",
    "SyncReport",
    "Vector",
    "default_retention",
    "documents_in",
    "store_for",
    "subject_hash",
]


def store_for(bus: EventBus) -> MemoryStore:
    """The memory store for a fabric: its own database, or memory for the in-memory fabric."""
    if isinstance(bus, PostgresEventBus):
        from aios.memory.postgres import PostgresMemoryStore  # noqa: PLC0415 - only with a database

        return PostgresMemoryStore(bus)
    return InMemoryMemoryStore()
