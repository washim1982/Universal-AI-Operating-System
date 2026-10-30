"""The Python workflow DSL (requirements §7.6). It compiles to the graph IR, the Workflow
manifest, which the engine executes; the DSL itself never runs anything.

    research_report = (
        Workflow("research-report", version="1.0.0")
        .then(Step("plan", capability="planning"))
        .then(Step.dynamic("research", from_plan="plan.output.tasks", capability="research.web"))
        .then(Step("review", capability="review", on_result={"reject": "plan"}, max_loops=2))
        .budget(max_cost_usd=3.00, max_duration="30m")
    )
    manifest = research_report.compile()
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import yaml

from aios.core.manifests import WorkflowManifest, parse_manifest


@dataclass(frozen=True)
class Step:
    """A task step (``capability``) or a tool step (``tool``)."""

    id: str
    capability: str | None = None
    tool: str | None = None
    input: Mapping[str, Any] | None = None
    needs: Sequence[str] | None = None
    timeout: str | None = None
    max_attempts: int = 3
    on_result: Mapping[str, str] | None = None
    max_loops: int = 0
    _dynamic: Mapping[str, Any] | None = field(default=None, repr=False)

    @classmethod
    def dynamic(
        cls,
        id: str,
        *,
        from_plan: str,
        capability: str,
        max_parallel: int = 5,
        needs: Sequence[str] | None = None,
        timeout: str | None = None,
        max_attempts: int = 3,
    ) -> Step:
        """Planner-driven fan-out: one task per item of ``from_plan`` (FR-WF-03)."""
        return cls(
            id,
            capability=capability,
            needs=needs,
            timeout=timeout,
            max_attempts=max_attempts,
            _dynamic={"fromPlan": from_plan, "maxParallel": max_parallel},
        )

    def ir(self) -> dict[str, Any]:
        if (self.capability is None) == (self.tool is None):
            raise ValueError(f"step {self.id!r} needs exactly one of capability or tool")
        node: dict[str, Any] = {"id": self.id, "maxAttempts": self.max_attempts}
        if self._dynamic is not None:
            node.update(type="dynamic", capability=self.capability, **self._dynamic)
        elif self.tool is not None:
            node.update(type="tool", tool=self.tool, input=dict(self.input or {}))
        else:
            node.update(type="task", capability=self.capability, input=dict(self.input or {}))
            if self.on_result:
                node.update(onResult=dict(self.on_result), maxLoops=self.max_loops)
        if self.needs is not None:
            node["needs"] = list(self.needs)
        if self.timeout is not None:
            node["timeout"] = self.timeout
        return node


@dataclass(frozen=True)
class Approval:
    """A human approval node (FR-HUM-01)."""

    id: str
    approvers: Sequence[str]
    timeout: str | None = None
    on_timeout: Literal["deny", "approve", "escalate"] = "deny"
    needs: Sequence[str] | None = None

    def ir(self) -> dict[str, Any]:
        node: dict[str, Any] = {
            "id": self.id,
            "type": "approval",
            "approvers": list(self.approvers),
            "onTimeout": self.on_timeout,
        }
        if self.timeout is not None:
            node["timeout"] = self.timeout
        if self.needs is not None:
            node["needs"] = list(self.needs)
        return node


@dataclass(frozen=True)
class Workflow:
    name: str
    version: str = "0.1.0"
    description: str = ""
    steps: tuple[Step | Approval, ...] = ()
    _budget: Mapping[str, Any] = field(default_factory=dict[str, Any])
    _output: str | None = None

    def then(self, step: Step | Approval) -> Workflow:
        return replace(self, steps=(*self.steps, step))

    def budget(
        self,
        *,
        max_cost_usd: float | None = None,
        max_duration: str | None = None,
        max_steps: int | None = None,
    ) -> Workflow:
        values = {"maxCostUSD": max_cost_usd, "maxDuration": max_duration, "maxSteps": max_steps}
        return replace(self, _budget={k: v for k, v in values.items() if v is not None})

    def output(self, expression: str) -> Workflow:
        """What the run returns, e.g. ``"${draft.output}"`` (default: the last step's output)."""
        return replace(self, _output=expression)

    def ir(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "description": self.description,
            "steps": [step.ir() for step in self.steps],
            "budget": dict(self._budget),
        }
        if self._output is not None:
            spec["output"] = self._output
        return {
            "apiVersion": "aios/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": self.name, "version": self.version},
            "spec": spec,
        }

    def compile(self) -> WorkflowManifest:
        """Validate and return the graph IR."""
        manifest = parse_manifest(self.ir())
        assert isinstance(manifest, WorkflowManifest)  # noqa: S101 - the IR says kind: Workflow
        return manifest

    def to_yaml(self) -> str:
        self.compile()
        return yaml.safe_dump(self.ir(), sort_keys=False)
