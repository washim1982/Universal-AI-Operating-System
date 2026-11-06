"""What Human OS keeps about an approval (FR-HUM-01 to 05; ADR-016).

An approval is one person's answer about one action. Its record starts as the
``approval.requested`` event, so what an approver reads is exactly what the log says was asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import JsonValue

from aios.core.catalog import ApprovalRequested
from aios.core.envelope import Event
from aios.core.identity import Identity

ApprovalStatus = Literal["pending", "granted", "denied", "expired", "cancelled"]
DEFAULT_APPROVAL_TIMEOUT = timedelta(hours=24)
"""Nothing waits for a person for ever: without a timeout, this applies, and silence means no."""


def selected(selector: str, by: Identity) -> bool:
    """Whether ``role:<name>``, ``user:<name>`` or a full principal names this caller."""
    kind, _, name = selector.partition(":")
    if kind == "role":
        return name in by.roles
    return selector in (by.principal, by.principal.split("@", 1)[0])


@dataclass(frozen=True, slots=True)
class Approval:
    request: Event
    """The ``approval.requested`` event."""
    details: ApprovalRequested
    status: ApprovalStatus = "pending"
    approved: bool = False
    """The action may go ahead: a person said yes, or a policy allows it after the timeout."""
    by: str | None = None
    comment: str | None = None
    decided_at: datetime | None = None

    @classmethod
    def of(cls, request: Event, **state: Any) -> Approval:
        return cls(request, ApprovalRequested.model_validate(request.data), **state)

    @property
    def id(self) -> str:
        return self.details.approval

    @property
    def tenant(self) -> str:
        return self.request.tenantid

    @property
    def app(self) -> str:
        return self.request.appid

    @property
    def run(self) -> str | None:
        return self.request.runid

    @property
    def initiator(self) -> str | None:
        """Who the action is for. They can't approve it (FR-HUM-04)."""
        return self.request.onbehalfof

    @property
    def expires_at(self) -> datetime:
        return self.details.expires_at

    def may_decide(self, by: Identity) -> bool:
        return by.principal != self.initiator and any(
            selected(selector, by) for selector in self.details.approvers
        )

    def view(self) -> dict[str, JsonValue]:
        """The record as the API shows it, with the way to the trace (FR-HUM-03)."""
        run = self.run
        return {
            **self.details.model_dump(mode="json"),
            "app": self.app,
            "run": run,
            "status": self.status,
            "approved": self.approved,
            "requested_by": self.request.principal,
            "initiator": self.initiator,
            "requested_at": self.request.time.isoformat(),
            "data_class": self.request.dataclass.value,
            "by": self.by,
            "comment": self.comment,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "trace": f"/v1/apps/{self.app}/runs/{run}/events" if run else None,
        }
