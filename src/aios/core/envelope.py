"""The AIOS event envelope: CloudEvents 1.0 plus AIOS extension attributes (ADR-002).

See requirements §7.1 and specs/event-envelope.md. Events are immutable. Agent code never sets
``tenantid`` or ``appid`` itself; the SDK and the fabric stamp them from the caller's scope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

from aios.core.ids import Ulid, new_ulid
from aios.core.topics import DNS_LABEL_PATTERN, EventKind, Topic, kind_of

SPEC_VERSION = "1.0"
ENVELOPE_SCHEMA_ID = "aios://schemas/envelope/1"

TenantId = Annotated[str, StringConstraints(pattern=rf"^{DNS_LABEL_PATTERN}$")]
AppId = Annotated[str, StringConstraints(pattern=rf"^{DNS_LABEL_PATTERN}$")]

PRINCIPAL_PATTERN = rf"^(user|agent|app|service|system):[A-Za-z0-9._~-]+(@{DNS_LABEL_PATTERN})?$"
Principal = Annotated[str, StringConstraints(pattern=PRINCIPAL_PATTERN)]
"""``<kind>:<name>[@<tenant>]``, e.g. ``agent:research-agent@acme`` or ``user:alice@acme``."""

TRACEPARENT_PATTERN = r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
TraceParent = Annotated[str, StringConstraints(pattern=TRACEPARENT_PATTERN)]
"""W3C Trace Context ``traceparent`` header value (version 00)."""


class DataClass(StrEnum):
    """Data classification labels, least to most sensitive (FR-SEC-11)."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    @property
    def rank(self) -> int:
        return list(DataClass).index(self)

    @staticmethod
    def strictest(*labels: DataClass) -> DataClass:
        """The most sensitive of ``labels``."""
        return max(labels, key=lambda label: label.rank)


class Event(BaseModel):
    """One immutable command or fact."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        title="AIOS Event Envelope",
        json_schema_extra={"$id": ENVELOPE_SCHEMA_ID},
    )

    # CloudEvents 1.0 context attributes
    specversion: str = Field(default=SPEC_VERSION, pattern=r"^1\.0$")
    id: Ulid = Field(default_factory=new_ulid, description="ULID; unique per event.")
    source: str = Field(
        min_length=1,
        pattern=r"^\S+$",
        description="URI-reference of the producer, e.g. aios://acme/apps/x/agents/y.",
    )
    type: Topic = Field(description="The logical topic, e.g. task.completed.")
    subject: str | None = Field(default=None, min_length=1)
    time: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    datacontenttype: str = "application/json"
    dataschema: str | None = Field(default=None, min_length=1)

    # AIOS extension attributes (lowercase alphanumeric names, as CloudEvents requires)
    tenantid: TenantId = Field(description="Set by the fabric/SDK from the caller's scope.")
    appid: AppId = Field(description="Set by the fabric/SDK; 'aios' for tenant-level events.")
    runid: Ulid | None = Field(default=None, description="The run this event belongs to.")
    correlationid: Ulid | None = Field(default=None, description="Groups a run or a conversation.")
    causationid: Ulid | None = Field(
        default=None, description="The event that directly caused this one."
    )
    hopcount: int = Field(
        default=0, ge=0, description="Causal depth, for loop protection (FR-EVT-11)."
    )
    principal: Principal = Field(description="Who acted.")
    onbehalfof: Principal | None = Field(default=None, description="For whom (FR-SEC-05).")
    dataclass: DataClass = Field(
        default=DataClass.INTERNAL, description="Classification (FR-SEC-11)."
    )
    traceparent: TraceParent | None = None
    idempotencykey: str | None = Field(default=None, min_length=1, max_length=255)

    data: JsonValue = None

    @field_validator("time")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def kind(self) -> EventKind:
        return kind_of(self.type)

    def derive(
        self,
        type: str,
        data: JsonValue = None,
        *,
        source: str,
        principal: str,
        dataclass: DataClass = DataClass.PUBLIC,
        subject: str | None = None,
        idempotencykey: str | None = None,
    ) -> Self:
        """Create an event caused by this one.

        The child keeps the tenant, app, run, correlation, delegation and trace context and the
        subject (unless ``subject`` is given), points ``causationid`` at this event and
        increments ``hopcount``. Its data class is the stricter of this event's and
        ``dataclass``: classification only propagates upwards.
        """
        fields: dict[str, Any] = {
            "source": source,
            "type": type,
            "subject": subject if subject is not None else self.subject,
            "tenantid": self.tenantid,
            "appid": self.appid,
            "runid": self.runid,
            "correlationid": self.correlationid,
            "causationid": self.id,
            "hopcount": self.hopcount + 1,
            "principal": principal,
            "onbehalfof": self.onbehalfof,
            "dataclass": DataClass.strictest(self.dataclass, dataclass),
            "traceparent": self.traceparent,
            "idempotencykey": idempotencykey,
            "data": data,
        }
        return self.__class__.model_validate(fields)
