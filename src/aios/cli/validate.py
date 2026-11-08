"""``aios validate <app-folder>``: everything that can be checked before AIOS runs it (WP 3.6).

It needs no server. It loads the App as ``aios dev`` would: its manifests, its policies (compiled to
Cedar), its configuration schema, and its agents' code. Then it checks what only shows up later
otherwise: a step that asks for a capability nothing provides, an agent no step uses, a tool an
agent lists but no policy allows.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from aios.controlplane import Runtime, load_app
from aios.core.manifests import (
    AgentManifest,
    AnyManifest,
    AppManifest,
    DynamicStep,
    PolicyManifest,
    TaskStep,
    WorkflowManifest,
)
from aios.core.topics import matches
from aios.fabric import InMemoryEventBus


@dataclass
class Report:
    app: str = ""
    errors: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    summary: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        lines = [f"error: {e}" for e in self.errors] + [f"warning: {w}" for w in self.warnings]
        lines.append(f"{'valid' if self.ok else 'not valid'}: {self.summary or self.app}")
        return "\n".join(lines)


def validate(folder: Path) -> Report:
    report = Report(app=str(folder))
    try:
        manifests = load_app(folder)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        report.errors.append(str(exc))
        return report
    apps = [m for m in manifests if isinstance(m, AppManifest)]
    if len(apps) != 1:
        report.errors.append(f"an App folder holds one App manifest; found {len(apps)}")
        return report
    [app] = apps
    report.app = f"{app.metadata.name} {app.metadata.version}"
    try:
        asyncio.run(_install(manifests))
    except Exception as exc:  # whatever stops an install stops it here, with its reason
        report.errors.append(f"{type(exc).__name__}: {exc}")
        return report
    agents = [m for m in manifests if isinstance(m, AgentManifest)]
    workflows = [m for m in manifests if isinstance(m, WorkflowManifest)]
    policies = [m for m in manifests if isinstance(m, PolicyManifest)]
    _check_capabilities(app, agents, workflows, report)
    _check_tools(agents, policies, report)
    report.summary = (
        f"{report.app}: {len(agents)} agent(s), {len(app.spec.humans)} done by people, "
        f"{len(workflows)} workflow(s), {len(policies)} policy(ies)"
    )
    return report


async def _install(manifests: list[AnyManifest]) -> None:
    async with InMemoryEventBus() as bus:
        Runtime(bus, tenant="validate").install(manifests)


def _check_capabilities(
    app: AppManifest,
    agents: list[AgentManifest],
    workflows: list[WorkflowManifest],
    report: Report,
) -> None:
    used = {a.metadata.name: False for a in agents if a.metadata.name in app.spec.agents}
    provided = {c.name: a.metadata.name for a in agents for c in a.spec.capabilities}
    provided |= {h.capability: "people" for h in app.spec.humans}
    for workflow in workflows:
        for step in workflow.spec.steps:
            if not isinstance(step, TaskStep | DynamicStep):
                continue
            wanted = step.capability
            who = [p for c, p in provided.items() if c == wanted or matches(wanted, c)]
            if not who:
                report.errors.append(
                    f"workflow {workflow.metadata.name}, step {step.id}: nothing provides "
                    f"{wanted!r} (no agent's capability, nothing in spec.humans)"
                )
            for name in who:
                used[name] = True
    for name, is_used in used.items():
        if not is_used:
            report.warnings.append(f"agent {name}: no workflow step asks for what it provides")


def _check_tools(
    agents: list[AgentManifest], policies: list[PolicyManifest], report: Report
) -> None:
    allowed = [
        str(rule.resource)
        for policy in policies
        for rule in policy.spec.allow
        if rule.action == "tool.invoke" and rule.resource is not None
    ]
    for agent in agents:
        for tool in agent.spec.tools.allow:
            if not any(tool == pattern or matches(pattern, tool) for pattern in allowed):
                report.warnings.append(
                    f"agent {agent.metadata.name}: {tool} is on its list, but no policy of this "
                    "App allows tool.invoke on it, so calls to it will be refused"
                )
