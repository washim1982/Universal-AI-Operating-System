"""Human OS: approvals, human tasks and notification channels (WP 2.4; ADR-016)."""

from aios.fabric.port import EventBus
from aios.fabric.postgres import PostgresEventBus
from aios.human.service import HumanService
from aios.human.store import ApprovalStore, InMemoryApprovalStore
from aios.human.webhook import ApprovalWebhook, signature, signed_by

__all__ = [
    "ApprovalStore",
    "ApprovalWebhook",
    "HumanService",
    "InMemoryApprovalStore",
    "signature",
    "signed_by",
    "store_for",
]


def store_for(bus: EventBus) -> ApprovalStore:
    """The approval store for a fabric: its own database, or memory for the in-memory fabric."""
    if isinstance(bus, PostgresEventBus):
        from aios.human.postgres import PostgresApprovalStore  # noqa: PLC0415 - needs a database

        return PostgresApprovalStore(bus)
    return InMemoryApprovalStore(bus)
