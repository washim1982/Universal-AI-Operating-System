"""An App as a tenant has it (ADR-019; FR-APP-03, 04, 05, 07).

An :class:`Installation` is the desired state, and it lives in the database, not in a process. Every
API and worker process converges on it, so a change reaches all of them and survives a restart. A
:class:`Bundle` is one immutable version of an App: its manifests as written, before configuration
fills them in, so that a change of configuration needs no new version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aios.core.envelope import DataClass
from aios.core.memory import MemoryScope
from aios.core.topics import matches

InstallStatus = Literal["awaiting-consent", "active", "uninstalled"]
DataChoice = Literal["retain", "delete"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _covered(wanted: str, patterns: list[str]) -> bool:
    return any(wanted == pattern or matches(pattern, wanted) for pattern in patterns)


class Grants(_Frozen):
    """What a tenant admin agreed to. What isn't here is refused at runtime (FR-APP-03)."""

    tools: list[str] = Field(default_factory=list[str])
    data_classes: list[DataClass] = Field(default_factory=list[DataClass])
    memory_scopes: list[MemoryScope] = Field(default_factory=list[MemoryScope])
    hosts: list[str] = Field(default_factory=list[str])

    def covers(self, wanted: Grants) -> bool:
        return (
            all(_covered(tool, self.tools) for tool in wanted.tools)
            and set(wanted.data_classes) <= set(self.data_classes)
            and set(wanted.memory_scopes) <= set(self.memory_scopes)
            and set(wanted.hosts) <= set(self.hosts)
        )

    def within(self, offered: Grants) -> bool:
        """Whether every grant is something the App asked for. Nobody grants what wasn't asked."""
        return offered.covers(self)

    def plus(self, other: Grants) -> Grants:
        return Grants(
            tools=sorted({*self.tools, *other.tools}),
            data_classes=sorted({*self.data_classes, *other.data_classes}),
            memory_scopes=sorted({*self.memory_scopes, *other.memory_scopes}),
            hosts=sorted({*self.hosts, *other.hosts}),
        )

    def without(self, other: Grants) -> Grants:
        return Grants(
            tools=[tool for tool in self.tools if tool not in other.tools],
            data_classes=[c for c in self.data_classes if c not in other.data_classes],
            memory_scopes=[s for s in self.memory_scopes if s not in other.memory_scopes],
            hosts=[host for host in self.hosts if host not in other.hosts],
        )

    def for_version(self, wanted: Grants) -> Grants:
        """What a version gets of these: what it asked for and was given, and never more."""
        return Grants(
            tools=[tool for tool in wanted.tools if _covered(tool, self.tools)],
            data_classes=[c for c in wanted.data_classes if c in self.data_classes],
            memory_scopes=[s for s in wanted.memory_scopes if s in self.memory_scopes],
            hosts=[host for host in wanted.hosts if host in self.hosts],
        )


class Requested(Grants):
    """What an App asks for, as a person sees it before agreeing."""

    per_run_usd: float | None = None
    monthly_usd: float | None = None
    starts: list[str] = Field(
        default_factory=list[str],
        description="What can start a run. Not for choosing, but worth knowing before agreeing.",
    )

    def grants(self) -> Grants:
        return Grants(
            tools=self.tools,
            data_classes=self.data_classes,
            memory_scopes=self.memory_scopes,
            hosts=self.hosts,
        )


class Bundle(_Frozen):
    """One version of an App, whole. ``digest`` covers everything else here."""

    name: str
    version: str
    digest: str
    documents: str = Field(description="Its manifests and policies, with ${config.*} unfilled")
    config_schema: dict[str, Any] | None = None
    code: str | None = Field(default=None, description="Its src/ folder: base64 of a .tar.gz")


class Installation(_Frozen):
    tenant: str
    app: str
    status: InstallStatus
    version: str | None = Field(default=None, description="What is running")
    pending: str | None = Field(default=None, description="What is waiting for consent")
    config: dict[str, Any] = Field(default_factory=dict[str, Any])
    pending_config: dict[str, Any] | None = None
    grants: Grants = Field(default_factory=Grants)
    refused: Grants = Field(
        default_factory=Grants,
        description="What a tenant admin said no to. It stays no until someone says otherwise.",
    )
    granted_by: str | None = None
    updated_at: datetime

    @property
    def running(self) -> bool:
        return self.status != "uninstalled" and self.version is not None
