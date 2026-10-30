"""Core types shared by every component: envelope, ids, topics, errors, catalog, manifests.

``aios.core`` depends on nothing else inside AIOS (ADR-001).
"""

from aios.core.envelope import DataClass, Event
from aios.core.errors import AIOSError
from aios.core.ids import is_ulid, new_ulid, ulid_time
from aios.core.topics import EventKind, kind_of, matches

__all__ = [
    "AIOSError",
    "DataClass",
    "Event",
    "EventKind",
    "is_ulid",
    "kind_of",
    "matches",
    "new_ulid",
    "ulid_time",
]
