"""Error types shared by every AIOS component.

Each error has a stable, machine-readable ``code`` that APIs, events and audit records use.
"""

from __future__ import annotations

from typing import ClassVar


class AIOSError(Exception):
    """Base class for all AIOS errors."""

    code: ClassVar[str] = "aios.error"


class InvalidTopicError(AIOSError, ValueError):
    """A topic, pattern, capability or identifier does not follow the topic grammar."""

    code = "topic.invalid"


class InvalidEventError(AIOSError, ValueError):
    """An event is malformed, or its payload does not match its catalog schema."""

    code = "event.invalid"


class InvalidPolicyError(AIOSError, ValueError):
    """A policy cannot be compiled to the policy engine or fails its schema validation."""

    code = "policy.invalid"


class HopLimitExceededError(AIOSError):
    """A causally derived event exceeded the hop limit (FR-EVT-11)."""

    code = "budget.exceeded"

    def __init__(self, hopcount: int, limit: int) -> None:
        super().__init__(f"hopcount {hopcount} exceeds the limit of {limit}")
        self.hopcount = hopcount
        self.limit = limit


class RequestTimeoutError(AIOSError, TimeoutError):
    """No reply arrived for a request within its timeout (FR-EVT-12)."""

    code = "request.timeout"


class SubscriptionConflictError(AIOSError):
    """A subscriber tried to join a consumer group with a different scope or pattern."""

    code = "fabric.subscription_conflict"


class BusClosedError(AIOSError):
    """The event bus has been closed."""

    code = "fabric.closed"


class NotFoundError(AIOSError, LookupError):
    """The requested resource does not exist, or is outside the caller's scope."""

    code = "not_found"
