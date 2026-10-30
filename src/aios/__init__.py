"""AIOS: an operating system for AI agents.

Agent authors write ``from aios import agent, Context`` (requirements §7.5). Those names are
loaded on first use, so importing a single component such as ``aios.core`` stays cheap.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

try:
    __version__ = version("aios")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

if TYPE_CHECKING:
    from aios.sdk import Approval, Context, Message, Step, Workflow, agent

__all__ = ["Approval", "Context", "Message", "Step", "Workflow", "__version__", "agent"]


def __getattr__(name: str) -> Any:
    if name in {"Approval", "Context", "Message", "Step", "Workflow", "agent"}:
        import aios.sdk  # noqa: PLC0415 - lazy, see the module docstring

        return getattr(aios.sdk, name)
    raise AttributeError(f"module 'aios' has no attribute {name!r}")
