"""What Human OS keeps about an approval (FR-HUM-01 to 05; ADR-016).

An approval is one person's answer about one action. Its record starts as the
``approval.requested`` event, so what an approver reads is exactly what the log says was asked.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from pydantic import JsonValue

from aios.core.catalog import ApprovalRequested, HumanTaskAssigned
from aios.core.envelope import Event
from aios.core.identity import Identity

ApprovalStatus = Literal["pending", "granted", "denied", "expired", "cancelled"]
TaskStatus = Literal["pending", "completed", "cancelled"]
DEFAULT_APPROVAL_TIMEOUT = timedelta(hours=24)
"""Nothing waits for a person for ever: without a timeout, this applies, and silence means no."""


Groups = Mapping[str, Collection[str]]
"""A tenant's groups: each name and its members' principals (FR-HUM-04)."""


def selected(selector: str, by: Identity, groups: Groups | None = None) -> bool:
    """Whether ``role:<name>``, ``group:<name>``, ``user:<name>`` or a full principal names this
    caller. A group is one the identity provider says the caller is in, or one of the tenant's."""
    kind, _, name = selector.partition(":")
    if kind == "role":
        return name in by.roles
    if kind == "group":
        return name in by.groups or by.principal in (groups or {}).get(name, ())
    return selector in (by.principal, by.principal.split("@", 1)[0])


@dataclass(frozen=True, slots=True)
class Vote:
    """One approver's yes, of the several a quorum needs."""

    by: str
    at: datetime
    comment: str | None = None


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
    votes: tuple[Vote, ...] = ()
    """The yeses so far, when it needs several (FR-HUM-04)."""
    level: int = 0
    """How many escalation steps have been taken (FR-HUM-05)."""
    reminders: int = 0
    reminded_at: datetime | None = None
    edits: Mapping[str, JsonValue] | None = None
    """What the approver changed in the action before saying yes (FR-HUM-06)."""

    @classmethod
    def of(
        cls, request: Event, *, progress: Mapping[str, Any] | None = None, **state: Any
    ) -> Approval:
        """An approval from its request, its state and its ``progress`` as stored."""
        found = progress or {}
        votes = tuple(
            Vote(str(v["by"]), datetime.fromisoformat(str(v["at"])), v.get("comment"))
            for v in cast(list[dict[str, Any]], found.get("votes") or [])
        )
        reminded = found.get("reminded_at")
        return cls(
            request,
            ApprovalRequested.model_validate(request.data),
            votes=votes,
            level=int(found.get("level") or 0),
            reminders=int(found.get("reminders") or 0),
            reminded_at=datetime.fromisoformat(str(reminded)) if reminded else None,
            edits=found.get("edits"),
            **state,
        )

    def progress(self) -> dict[str, JsonValue]:
        """What changes while it is pending, as the store keeps it."""
        return {
            "votes": [
                {"by": v.by, "at": v.at.isoformat(), "comment": v.comment} for v in self.votes
            ],
            "level": self.level,
            "reminders": self.reminders,
            "reminded_at": self.reminded_at.isoformat() if self.reminded_at else None,
            "edits": dict(self.edits) if self.edits is not None else None,
        }

    @property
    def quorum(self) -> int:
        return self.details.quorum

    @property
    def approvers(self) -> tuple[str, ...]:
        """Who may answer now: those first asked, and every escalation step taken so far."""
        later = [a for step in self.details.escalation[: self.level] for a in step.approvers]
        return tuple(dict.fromkeys([*self.details.approvers, *later]))

    @property
    def voted(self) -> tuple[str, ...]:
        return tuple(vote.by for vote in self.votes)

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

    def may_decide(self, by: Identity, groups: Groups | None = None) -> bool:
        """Whether ``by`` may answer it now. Nobody approves an action for themselves, and nobody
        counts twice towards a quorum."""
        return (
            by.principal != self.initiator
            and by.principal not in self.voted
            and any(selected(selector, by, groups) for selector in self.approvers)
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
            "approvers": list(self.approvers),
            "votes": [{"by": v.by, "comment": v.comment} for v in self.votes],
            "level": self.level,
            "edits": dict(self.edits) if self.edits is not None else None,
            "requested_by": self.request.principal,
            "initiator": self.initiator,
            "requested_at": self.request.time.isoformat(),
            "data_class": self.request.dataclass.value,
            "by": self.by,
            "comment": self.comment,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "trace": f"/v1/apps/{self.app}/runs/{run}/events" if run else None,
        }


@dataclass(frozen=True, slots=True)
class HumanTask:
    """Work people do for a run, as an agent would (FR-HUM-08). Its record starts as the
    ``task.requested`` event, so the answer goes back to the run as an agent's does."""

    request: Event
    """The ``task.requested`` event."""
    details: HumanTaskAssigned
    status: TaskStatus = "pending"
    by: str | None = None
    output: JsonValue = None
    comment: str | None = None
    completed_at: datetime | None = None

    @property
    def id(self) -> str:
        return self.request.id

    @property
    def tenant(self) -> str:
        return self.request.tenantid

    @property
    def app(self) -> str:
        return self.request.appid

    @property
    def run(self) -> str | None:
        return self.request.runid

    def may_do(self, by: Identity, groups: Groups | None = None) -> bool:
        return any(selected(selector, by, groups) for selector in self.details.assignees)

    def view(self) -> dict[str, JsonValue]:
        run = self.run
        return {
            **self.details.model_dump(mode="json"),
            "app": self.app,
            "run": run,
            "status": self.status,
            "requested_at": self.request.time.isoformat(),
            "tainted": self.request.tainted,
            "by": self.by,
            "output": self.output,
            "comment": self.comment,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "trace": f"/v1/apps/{self.app}/runs/{run}/events" if run else None,
        }
