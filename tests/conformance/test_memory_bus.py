"""The in-memory adapter against the shared EventBus conformance suite."""

from collections.abc import AsyncIterator

import pytest

from aios.fabric import EventBus, InMemoryEventBus
from aios.testing.fabric import EventBusConformance


class TestInMemoryEventBus(EventBusConformance):
    @pytest.fixture
    async def bus(self) -> AsyncIterator[EventBus]:
        async with InMemoryEventBus() as bus:
            yield bus
