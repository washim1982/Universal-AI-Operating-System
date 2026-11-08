"""The OpenAI Agents SDK agent of Aios Template App.

AIOS calls ``build`` for each task, with the AIOS tools the agent's manifest names in
``tools.allow``. Every model call goes through the Model Kernel, whatever model is named here.
"""

from typing import Any

from agents import Agent, FunctionTool


def build(tools: list[FunctionTool]) -> Agent[Any]:
    return Agent(name="Assistant", instructions="Answer briefly and accurately.", tools=list(tools))
