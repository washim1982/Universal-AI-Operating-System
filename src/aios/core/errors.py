"""Error types shared by every AIOS component.

Each error has a stable, machine-readable ``code`` that APIs, events and audit records use.
"""

from __future__ import annotations

from typing import ClassVar


class AIOSError(Exception):
    """Base class for all AIOS errors."""

    code: ClassVar[str] = "aios.error"


class ReplayDivergedError(AIOSError):
    """A replay made a call its source never made: the agent no longer does what it did."""

    code = "replay.diverged"


class RecordedError(AIOSError):
    """A failure met again in a replay, where the source met it. Its code is the source's."""

    code = "replay.recorded"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


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


class AuthenticationError(AIOSError):
    """The credential is missing, invalid, expired or revoked (FR-SEC-02)."""

    code = "auth.invalid_credential"


class PolicyDeniedError(AIOSError):
    """A policy denied a governed action other than a tool call (FR-SEC-04, FR-SEC-13)."""

    code = "policy.denied"


class MemoryConflictError(AIOSError):
    """An update named a version that is no longer the item's current one (FR-MEM-02)."""

    code = "memory.conflict"


class ReservedEventError(AIOSError):
    """Agent code tried to publish an event that only the platform may publish."""

    code = "event.reserved"


class ConflictError(AIOSError):
    """The request doesn't fit the state things are in."""

    code = "conflict"


class BudgetExceededError(AIOSError):
    """A step, token, cost or time budget ran out (FR-AGT-08, FR-WF-08, FR-MOD-07, FR-MOD-08)."""

    code = "budget.exceeded"

    def __init__(
        self, budget: str, limit: float, observed: float, *, level: str | None = None
    ) -> None:
        whose = f"{level} {budget}" if level is not None else budget
        super().__init__(f"{whose} budget exceeded: {observed:g} > {limit:g}")
        self.budget = budget
        self.limit = limit
        self.observed = observed
        self.level = level


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


class ToolCredentialError(ToolError):
    """A tool's credential is not set, or this App did not declare it (FR-TOOL-06)."""

    code = "tool.credential_missing"


class ToolDeniedError(ToolError):
    """The policy denied the call; the model can't grant itself permissions (FR-SEC-04)."""

    code = "policy.denied"


class EgressDeniedError(ToolDeniedError):
    """The call would reach a host that isn't on its allowlist (FR-SEC-10)."""

    code = "egress.denied"


class ApprovalRequiredError(ToolError):
    """The policy requires an approval, and nothing here can wait for one.

    A subscription has no run step to pause, and a component without Human OS has nobody to ask.
    """

    code = "approval.required"

    def __init__(self, message: str, approvers: tuple[str, ...]) -> None:
        super().__init__(message)
        self.approvers = approvers


class ApprovalDeniedError(ToolDeniedError):
    """A person said no. An agent can catch this and answer some other way (M2 exit 2)."""

    code = "approval.denied"


class ApprovalExpiredError(ApprovalDeniedError):
    """Nobody answered in time, which counts as no (FR-HUM-05)."""

    code = "approval.expired"


class ApprovalClosedError(AIOSError):
    """The approval has already been decided, or has expired or been cancelled."""

    code = "approval.closed"


class ApprovalForbiddenError(AIOSError):
    """This caller isn't one of the approvers, or started the action (FR-HUM-04)."""

    code = "approval.forbidden"


class AwaitingApproval(BaseException):
    """Stops a task until a person decides; the run pauses and nothing waits in memory.

    Not an ``Exception``, for the reason ``asyncio.CancelledError`` isn't one. A handler with
    ``except Exception`` around a tool call would otherwise carry on as if the call had failed, and
    could finish the task, or do something else, with the approval still open.
    """

    def __init__(self, approval: str) -> None:
        super().__init__(f"waiting for approval {approval}")
        self.approval = approval
