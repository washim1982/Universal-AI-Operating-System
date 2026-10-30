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


class BudgetExceededError(AIOSError):
    """A step, token, cost or time budget ran out (FR-AGT-08, FR-WF-08, FR-MOD-07)."""

    code = "budget.exceeded"

    def __init__(self, budget: str, limit: float, observed: float) -> None:
        super().__init__(f"{budget} budget exceeded: {observed:g} > {limit:g}")
        self.budget = budget
        self.limit = limit
        self.observed = observed


class ModelError(AIOSError):
    """No candidate model produced a valid response (FR-MOD-05)."""

    code = "model.failed"


class ModelOutputError(ModelError):
    """The model's output does not match the requested structured-output schema."""

    code = "model.invalid_output"


class ToolError(AIOSError):
    """A tool call failed (FR-TOOL-05). ``retryable`` tells the caller whether to try again."""

    code = "tool.failed"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ToolNotFoundError(ToolError):
    code = "tool.not_found"


class ToolInputError(ToolError):
    code = "tool.invalid_input"


class ToolDeniedError(ToolError):
    """The policy denied the call; the model can't grant itself permissions (FR-SEC-04)."""

    code = "policy.denied"


class ApprovalRequiredError(ToolError):
    """The policy requires an approval first. Pausing for it arrives with Human OS (WP 2.4)."""

    code = "approval.required"

    def __init__(self, message: str, approvers: tuple[str, ...]) -> None:
        super().__init__(message)
        self.approvers = approvers
