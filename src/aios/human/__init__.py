"""Human OS: approvals, human tasks and notification channels (WP 2.4; ADR-016)."""

from aios.fabric.port import EventBus
from aios.fabric.postgres import PostgresEventBus
from aios.human.channels import (
    ActionLink,
    ActionLinks,
    ApprovalNotifier,
    Channel,
    Contact,
    EmailChannel,
    SlackChannel,
    Smtp,
    TeamsChannel,
)
from aios.human.service import HumanService
from aios.human.store import ApprovalStore, InMemoryApprovalStore
from aios.human.tasks import InMemoryTaskStore, TaskStore
from aios.human.webhook import ApprovalWebhook, signature, signed_by

__all__ = [
    "ActionLink",
    "ActionLinks",
    "ApprovalNotifier",
    "ApprovalStore",
    "ApprovalWebhook",
    "Channel",
    "Contact",
    "EmailChannel",
    "HumanService",
    "InMemoryApprovalStore",
    "InMemoryTaskStore",
    "SlackChannel",
    "Smtp",
    "TaskStore",
    "TeamsChannel",
    "signature",
    "signed_by",
    "store_for",
    "tasks_for",
]


def store_for(bus: EventBus) -> ApprovalStore:
    """The approval store for a fabric: its own database, or memory for the in-memory fabric."""
    if isinstance(bus, PostgresEventBus):
        from aios.human.postgres import PostgresApprovalStore  # noqa: PLC0415 - needs a database

        return PostgresApprovalStore(bus)
    return InMemoryApprovalStore(bus)


def tasks_for(bus: EventBus) -> TaskStore:
    """Where work for people is kept: the fabric's database, or memory with the in-memory fabric."""
    if isinstance(bus, PostgresEventBus):
        from aios.human.postgres import PostgresTaskStore  # noqa: PLC0415 - needs a database

        return PostgresTaskStore(bus)
    return InMemoryTaskStore(bus)
