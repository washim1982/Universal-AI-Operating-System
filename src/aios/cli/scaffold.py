"""``aios init``: a new App from a template, or a new agent or tool in one (WP 3.6; FR-CP-06).

    aios init my-app --template rag
    aios init agent reviewer --app my-app --template langgraph
    aios init tool orders.refund.create --app my-app

Templates are folders under ``aios/cli/templates``, written with the names ``aios-template-app``,
``aios_template_app`` and ``Aios Template App``, which become the App's name, package and title.
Every template has a test that passes as it is, with a stand-in model.
"""

from __future__ import annotations

import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from aios.core.topics import is_dns_label

TEMPLATES = ("chat", "rag", "approval", "people", "langgraph", "openai-agents")
AGENT_TEMPLATES = ("python", "langgraph", "openai-agents")
NAME, PACKAGE, TITLE = "aios-template-app", "aios_template_app", "Aios Template App"
TOOL_NAME = re.compile(r"[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*")
TEXT = (".py", ".yaml", ".yml", ".md", ".toml", ".json", ".txt")


def init(name: str, directory: Path, template: str = "chat") -> Path:
    """A new App, ``directory/name``, from ``template``."""
    if not is_dns_label(name):
        raise SystemExit(f"App names are lowercase letters, digits and hyphens: {name!r}")
    if template not in TEMPLATES:
        raise SystemExit(f"no template {template!r}; there are {', '.join(TEMPLATES)}")
    root = directory / name
    if root.exists():
        raise SystemExit(f"{root} already exists")
    package = name.replace("-", "_")
    title = name.replace("-", " ").title()
    source = resources.files("aios.cli").joinpath("templates", template)
    for relative, data in _files(source):
        target = root / relative.replace(PACKAGE, package)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix in TEXT:
            text = data.decode("utf-8")
            text = text.replace(NAME, name).replace(PACKAGE, package).replace(TITLE, title)
            target.write_text(text, encoding="utf-8", newline="\n")
        else:
            target.write_bytes(data)
    return root


def _files(folder: Traversable, prefix: str = "") -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if entry.name == "__pycache__":
            continue
        relative = f"{prefix}{entry.name}"
        if entry.is_dir():
            found += _files(entry, f"{relative}/")
        else:
            found.append((relative, entry.read_bytes()))
    return found


# ---------------------------------------------------------------------- inside an App


def _package(app: Path) -> tuple[str, Path]:
    """The App's name and its Python package folder."""
    manifest = app / "manifests" / "app.yaml"
    if not manifest.is_file():
        raise SystemExit(f"{app} has no manifests/app.yaml: is it an App folder?")
    found = re.search(r"^metadata:\s*\{?\s*name:\s*([a-z0-9-]+)", _read(manifest), re.M)
    if found is None:
        raise SystemExit(f"{manifest} doesn't say the App's name in metadata")
    packages = [p for p in (app / "src").glob("*") if (p / "__init__.py").is_file()]
    if len(packages) != 1:
        raise SystemExit(f"{app / 'src'} should hold one Python package; found {len(packages)}")
    return found[1], packages[0]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


AgentTemplate = Literal["python", "langgraph", "openai-agents"]


def add_agent(
    name: str, app: Path, template: str = "python", capability: str | None = None
) -> list[str]:
    """A new agent in the App at ``app``: its manifest and its code. Says what was done."""
    if not is_dns_label(name):
        raise SystemExit(f"agent names are lowercase letters, digits and hyphens: {name!r}")
    if template not in AGENT_TEMPLATES:
        raise SystemExit(f"no agent template {template!r}; there are {', '.join(AGENT_TEMPLATES)}")
    app_name, package = _package(app)
    capability = capability or name.removesuffix("-agent")
    module = name.replace("-", "_")
    manifest = app / "manifests" / f"{name}.yaml"
    code = package / f"{module}.py"
    for path in (manifest, code):
        if path.exists():
            raise SystemExit(f"{path} already exists")
    runtime, attribute, body = _AGENT_CODE[template]
    entrypoint = f"{package.name}.{module}:{attribute}"
    manifest.write_text(
        _AGENT_MANIFEST.format(
            name=name, runtime=runtime, entrypoint=entrypoint, capability=capability
        ),
        encoding="utf-8",
        newline="\n",
    )
    code.write_text(
        body.format(name=name, capability=capability, app=app_name), encoding="utf-8", newline="\n"
    )
    done = [f"created {manifest.as_posix()}", f"created {code.as_posix()}"]
    listed = _listed(app / "manifests" / "app.yaml", name)
    done.append(
        f"added {name} to spec.agents in manifests/app.yaml"
        if listed
        else f"add {name} to spec.agents in manifests/app.yaml"
    )
    done.append(
        f"give it a workflow step: - {{id: {module}, type: task, capability: {capability}}}"
    )
    return done


def _listed(manifest: Path, agent: str) -> bool:
    """Add ``agent`` to an App's ``agents: [...]`` line, when it is written on one line."""
    text = _read(manifest)
    line = re.search(r"^(\s+agents:\s*\[)([^\]\n]*)(\])", text, re.M)
    if line is None:
        return False
    names = [n.strip() for n in line[2].split(",") if n.strip()]
    if agent not in names:
        names.append(agent)
    replaced = f"{line[1]}{', '.join(names)}{line[3]}"
    manifest.write_text(text[: line.start()] + replaced + text[line.end() :], encoding="utf-8")
    return True


def add_tool(name: str, app: Path) -> list[str]:
    """A new native tool in the App's ``tools.py``. Says what was done, and what is left."""
    if not TOOL_NAME.fullmatch(name):
        raise SystemExit(f"a tool's name is three parts, like orders.refund.create: {name!r}")
    _, package = _package(app)
    function = name.rsplit(".", maxsplit=1)[-1].replace("-", "_")
    provider = name.split(".", maxsplit=1)[0]
    tools = package / "tools.py"
    done: list[str] = []
    if tools.exists():
        text = _read(tools)
        if f'"{name}"' in text:
            raise SystemExit(f"{tools} has {name} already")
        tools.write_text(text.rstrip() + "\n\n" + _TOOL.format(name=name, function=function))
        done.append(f"added {name} to {tools.as_posix()}")
        done.append(f"add {function} to a provider in providers() there")
    else:
        tools.write_text(
            _TOOLS_HEAD.format(package=package.name)
            + _TOOL.format(name=name, function=function)
            + _PROVIDERS.format(provider=provider, function=function),
            encoding="utf-8",
            newline="\n",
        )
        done.append(f"created {tools.as_posix()}")
    done += [
        f'let an agent use it: tools: {{allow: ["{name}"]}} in its manifest',
        f"allow it in a policy: allow: [{{action: tool.invoke, resource: {name}}}]",
        f"start AIOS with it: AIOS_TOOL_PROVIDERS={package.name}.tools:providers aios dev --app .",
    ]
    return done


_AGENT_MANIFEST = """apiVersion: aios/v1alpha1
kind: Agent
metadata: {{name: {name}, version: 0.1.0}}
spec:
  description: What {name} does.
  runtime: {{type: "{runtime}", entrypoint: "{entrypoint}"}}
  capabilities: [{{name: {capability}}}]
  model: {{task: reasoning}}
  tools: {{allow: []}}
  limits: {{maxSteps: 10, maxCostUSD: 0.50, timeout: 2m}}
"""

_PYTHON_AGENT = '''"""{name}, an agent of {app}."""

from pydantic import BaseModel

from aios import Context, agent


class Task(BaseModel):
    message: str


class Result(BaseModel):
    answer: str


@agent("{name}", capability="{capability}")
async def run(ctx: Context, task: Task) -> Result:
    return await ctx.model.generate(
        task="reasoning",
        system="Do the task briefly and accurately.",
        messages=[ctx.user(task.message)],
        output=Result,
    )
'''

_LANGGRAPH_AGENT = '''"""{name}, a LangGraph agent of {app}. AIOS calls build() for each task."""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition


def build(model: BaseChatModel, tools: list[BaseTool]) -> Any:
    llm = model.bind_tools(tools) if tools else model

    async def think(state: MessagesState) -> dict[str, Any]:
        return {{"messages": [await llm.ainvoke(state["messages"])]}}

    graph = StateGraph(MessagesState)
    graph.add_node("think", think)
    graph.add_edge(START, "think")
    if tools:
        graph.add_node("tools", ToolNode(tools))
        graph.add_conditional_edges("think", tools_condition)
        graph.add_edge("tools", "think")
    else:
        graph.add_edge("think", END)
    return graph.compile()
'''

_OPENAI_AGENT = '''"""{name}, an OpenAI Agents SDK agent of {app}.

AIOS calls build() for each task.
"""

from typing import Any

from agents import Agent, FunctionTool


def build(tools: list[FunctionTool]) -> Agent[Any]:
    instructions = "Do the task briefly and accurately."
    return Agent(name="{name}", instructions=instructions, tools=list(tools))
'''

_AGENT_CODE: dict[str, tuple[str, str, str]] = {
    "python": ("python", "run", _PYTHON_AGENT),
    "langgraph": ("adapter:langgraph", "build", _LANGGRAPH_AGENT),
    "openai-agents": ("adapter:openai-agents", "build", _OPENAI_AGENT),
}

_TOOLS_HEAD = '''"""The App's own tools. Start AIOS with them:

AIOS_TOOL_PROVIDERS={package}.tools:providers
"""

from typing import Any

from aios.core.policy import SideEffect
from aios.tools import NativeProvider, ToolProvider, tool

'''

_TOOL = '''
@tool("{name}", side_effect=SideEffect.READ, description="What {name} does.")
async def {function}(text: str) -> dict[str, Any]:
    """Say what it does; the side effect says what policy and approvals treat it as."""
    return {{"echo": text}}
'''

_PROVIDERS = """

def providers() -> list[ToolProvider]:
    return [NativeProvider("{provider}", [{function}])]
"""
