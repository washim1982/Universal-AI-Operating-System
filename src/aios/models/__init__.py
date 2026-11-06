"""Model Kernel: registry, routing, provider adapters, accounting and cassettes (WP 1.4)."""

from aios.fabric.port import EventBus
from aios.fabric.postgres import PostgresEventBus
from aios.models.kernel import DEFAULT_POLICY, ModelKernel, default_policy
from aios.models.ledger import InMemorySpendLedger, SpendLedger
from aios.models.providers import (
    AnthropicProvider,
    Completion,
    Embedding,
    OpenAIProvider,
    Provider,
    ProviderError,
)
from aios.models.registry import ModelEntry, ModelRegistry, Prices
from aios.models.router import Route, Router

__all__ = [
    "DEFAULT_POLICY",
    "AnthropicProvider",
    "Completion",
    "Embedding",
    "InMemorySpendLedger",
    "ModelEntry",
    "ModelKernel",
    "ModelRegistry",
    "OpenAIProvider",
    "Prices",
    "Provider",
    "ProviderError",
    "Route",
    "Router",
    "SpendLedger",
    "default_policy",
    "ledger_for",
]


def ledger_for(bus: EventBus) -> SpendLedger:
    """The spend ledger for a fabric: its own database, or memory for the in-memory fabric."""
    if isinstance(bus, PostgresEventBus):
        from aios.models.postgres import PostgresSpendLedger  # noqa: PLC0415 - needs a database

        return PostgresSpendLedger(bus)
    return InMemorySpendLedger(bus)
