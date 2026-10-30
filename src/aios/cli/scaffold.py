"""``aios init``: a new App from a template (WP 1.7; NFR "first agent in 15 minutes")."""

from __future__ import annotations

from pathlib import Path

from aios.core.topics import is_dns_label

_APP = """apiVersion: aios/v1alpha1
kind: App
metadata: {{ name: {name}, version: 0.1.0 }}
spec:
  description: {title} - an AIOS App.
  entrypoints:
    - type: chat
      workflow: answer
  agents: [assistant-agent]
  workflows: [answer]
  modelPolicy: default
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {{ name: assistant-agent, version: 0.1.0 }}
spec:
  description: Answers the user's message.
  runtime: {{ type: python, entrypoint: {package}.agents:answer }}
  capabilities: [{{ name: answering }}]
  model: {{ task: reasoning }}
  limits: {{ maxSteps: 5, maxCostUSD: 0.50, timeout: 2m }}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {{ name: answer, version: 0.1.0 }}
spec:
  steps:
    - id: answer
      type: task
      capability: answering
      input: {{ message: "${{input.message}}" }}
"""

_AGENTS = '''"""Agents of the {title} App."""

from pydantic import BaseModel

from aios import Context, agent


class Question(BaseModel):
    message: str


class Answer(BaseModel):
    answer: str


@agent("assistant-agent", capability="answering")
async def answer(ctx: Context, task: Question) -> Answer:
    return await ctx.model.generate(
        task="reasoning",
        system="Answer briefly and accurately.",
        messages=[ctx.user(task.message)],
        output=Answer,
    )
'''


def init(name: str, directory: Path) -> Path:
    if not is_dns_label(name):
        raise SystemExit(f"App names are lowercase letters, digits and hyphens: {name!r}")
    root = directory / name
    if root.exists():
        raise SystemExit(f"{root} already exists")
    package = name.replace("-", "_")
    title = name.replace("-", " ").title()
    (root / "manifests").mkdir(parents=True)
    (root / "src" / package).mkdir(parents=True)
    (root / "manifests" / "app.yaml").write_text(
        _APP.format(name=name, title=title, package=package), encoding="utf-8"
    )
    (root / "src" / package / "__init__.py").write_text(f'"""{title}."""\n', encoding="utf-8")
    (root / "src" / package / "agents.py").write_text(_AGENTS.format(title=title), encoding="utf-8")
    (root / "README.md").write_text(
        f'# {title}\n\n```bash\naios dev --app {root.as_posix()}\naios run {name} "Hello"\n```\n',
        encoding="utf-8",
    )
    return root
