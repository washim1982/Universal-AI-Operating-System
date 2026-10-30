"""Event Fabric: the ``EventBus`` port and its adapters (FR-EVT-01 to FR-EVT-17).

``aios.fabric`` depends only on ``aios.core`` (ADR-001).
"""

from aios.fabric.memory import InMemoryEventBus
from aios.fabric.port import (
    ANY,
    DeadLetter,
    Delivery,
    EventBus,
    RetryPolicy,
    Scope,
    StartFrom,
    StoredEvent,
    Subscription,
)

__all__ = [
    "ANY",
    "DeadLetter",
    "Delivery",
    "EventBus",
    "InMemoryEventBus",
    "RetryPolicy",
    "Scope",
    "StartFrom",
    "StoredEvent",
    "Subscription",
]
