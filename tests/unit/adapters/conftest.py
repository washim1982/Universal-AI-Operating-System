"""A help desk whose agent is another framework's, with one AIOS tool and a scripted model."""

import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

from aios.core.catalog import ModelToolCall
from aios.core.manifests import load_manifests
from aios.core.policy import SideEffect
from aios.core.ports import ModelRequest
from aios.models import Completion, ModelEntry, Provider
from aios.testing.runtime import LocalRuntime
from aios.tools import NativeProvider, tool

DESK = """
apiVersion: aios/v1alpha1
kind: App
metadata: {{name: helpdesk, version: 1.0.0}}
spec:
  entrypoints: [{{type: chat, workflow: answer}}]
  agents: [helper]
  workflows: [answer]
  policies: [articles]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {{name: helper, version: 1.0.0}}
spec:
  runtime: {{type: "adapter:{framework}", entrypoint: "{entrypoint}"}}
  capabilities: [{{name: help.answer}}]
  model: {{task: planning}}
  tools: {{allow: ["kb.article.find"]}}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {{name: answer, version: 1.0.0}}
spec:
  output: "${{answer.output}}"
  steps:
    - {{id: answer, type: task, capability: help.answer, input: {{message: "${{input.message}}"}}}}
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {{name: articles, version: 1.0.0}}
spec:
  subject: {{app: helpdesk}}
  allow:
    - {{action: tool.invoke, resource: "kb.>"}}
"""


@dataclass
class Scripted(Provider):
    """Asks for the article tool once, then answers from what it returned."""

    name: str = "anthropic"
    requests: list[ModelRequest] = field(default_factory=list[ModelRequest])
    answer: str = "Refunds take 30 days."

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.requests.append(request)
        if request.messages[-1].role == "tool" or not request.tools:
            return Completion(text=self.answer, input_tokens=20, output_tokens=6, model=entry.model)
        call = ModelToolCall(id="toolu_1", name=request.tools[0].name, arguments={"q": "refunds"})
        return Completion(
            text="", input_tokens=10, output_tokens=4, model=entry.model, tool_calls=(call,)
        )


@dataclass
class Desk:
    runtime: LocalRuntime
    model: Scripted
    asked: list[str]


Make = Callable[..., Any]


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, dict[str, Any]], str]:
    """Put names in an importable module, and say where they are."""

    def put(name: str, contents: dict[str, Any]) -> str:
        found = ModuleType(name)
        for key, value in contents.items():
            setattr(found, key, value)
        monkeypatch.setitem(sys.modules, name, found)
        return name

    return put


@pytest.fixture
async def desk() -> AsyncIterator[Callable[..., Any]]:
    running: list[LocalRuntime] = []

    async def make(framework: str, entrypoint: str, model: Scripted | None = None) -> Desk:
        scripted = model or Scripted()
        asked: list[str] = []

        @tool("kb.article.find", side_effect=SideEffect.READ, description="Find help articles.")
        async def find(q: str) -> dict[str, Any]:
            asked.append(q)
            return {"days": 30}

        runtime = LocalRuntime(tenant="acme", providers={"anthropic": scripted})
        await runtime.__aenter__()
        running.append(runtime)
        await runtime.runtime.register_tool_provider(NativeProvider("kb", [find]))
        manifests = load_manifests(DESK.format(framework=framework, entrypoint=entrypoint))
        await runtime.runtime.apply(manifests, tenant="acme")
        return Desk(runtime, scripted, asked)

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)
