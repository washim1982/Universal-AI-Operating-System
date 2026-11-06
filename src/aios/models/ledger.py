"""What has been spent, so that a limit can be checked before a call and not after (ADR-018).

The log says what every call cost, but adding it up again for each new call can't answer "what has
this tenant spent this month". So :meth:`SpendLedger.charge` adds a call to each thing paying for
it and publishes the call, in one step: a call is never on the record without being paid for.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from aios.core.catalog import BudgetLevel
from aios.core.envelope import Event
from aios.core.ports import CallSite
from aios.fabric.port import EventBus


def subjects_of(site: CallSite, now: datetime) -> dict[BudgetLevel, str]:
    """Who pays for a call. Monthly totals start again at each month, in UTC."""
    event = site.event
    month = f"{now:%Y-%m}"
    paying: dict[BudgetLevel, str] = {
        "app": f"app:{event.appid}:{month}",
        "tenant": f"tenant:{month}",
    }
    if event.runid is not None:
        paying["run"] = f"run:{event.runid}"
    if event.onbehalfof is not None:
        paying["user"] = f"user:{event.onbehalfof}:{month}"
    return paying


class SpendLedger(ABC):
    @abstractmethod
    async def spent(self, tenant: str, subjects: Sequence[str]) -> dict[str, float]:
        """What each subject has spent, in USD. A subject that hasn't spent anything is left out."""

    @abstractmethod
    async def charge(self, tenant: str, subjects: Sequence[str], usd: float, event: Event) -> None:
        """Add ``usd`` to each subject and publish ``event``, as one atomic step."""

    @abstractmethod
    async def warn_once(self, tenant: str, subject: str, event: Event) -> bool:
        """Publish ``event`` unless this subject has been warned before; ``True`` if it hadn't."""


class InMemorySpendLedger(SpendLedger):
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._spent: dict[tuple[str, str], float] = {}
        self._warned: set[tuple[str, str]] = set()

    async def spent(self, tenant: str, subjects: Sequence[str]) -> dict[str, float]:
        return {s: self._spent[(tenant, s)] for s in subjects if (tenant, s) in self._spent}

    async def charge(self, tenant: str, subjects: Sequence[str], usd: float, event: Event) -> None:
        await self.bus.publish(event)
        if usd > 0:
            for subject in subjects:
                self._spent[(tenant, subject)] = self._spent.get((tenant, subject), 0.0) + usd

    async def warn_once(self, tenant: str, subject: str, event: Event) -> bool:
        if (tenant, subject) in self._warned:
            return False
        self._warned.add((tenant, subject))
        await self.bus.publish(event)
        return True
