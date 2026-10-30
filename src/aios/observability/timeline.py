"""The run timeline: a run's events as a causal tree (FR-OBS-02, 03; ``aios trace``).

Every event of a run except the request that started it has a ``causationid`` inside the run,
so the run is one tree. Rendering it shows who did what, in which order, how long it took
and what it cost.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aios.core.envelope import Event


@dataclass
class Node:
    event: Event
    children: list[Node] = field(default_factory=list["Node"])


def build(events: Sequence[Event]) -> list[Node]:
    """The causal forest of ``events`` (one root per run, normally)."""
    nodes = {event.id: Node(event) for event in events}
    roots: list[Node] = []
    for event in events:
        parent = nodes.get(event.causationid or "")
        (parent.children if parent is not None else roots).append(nodes[event.id])
    return roots


def _summary(event: Event) -> str:
    data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
    usage: dict[str, Any] = data.get("usage") or {}
    parts: list[str] = []
    for key in ("step", "capability", "tool", "agent"):
        if data.get(key) is not None:
            parts.append(f"{key}={data[key]}")
    if data.get("item") is not None:
        parts.append(f"item={data['item']}")
    if event.type == "model.call.completed":
        parts.append(f"{data.get('provider')}/{data.get('model')}")
        parts.append(f"{usage.get('input_tokens', 0)}+{usage.get('output_tokens', 0)} tok")
        parts.append(f"${usage.get('cost_usd', 0):.4f}")
        parts.append(f"{data.get('latency_ms', 0):.0f}ms")
    elif event.type in ("run.completed", "run.failed", "run.cancelled") and usage:
        parts.append(f"${usage.get('cost_usd', 0):.4f} total")
    if isinstance(data.get("error"), dict):
        parts.append(f"error={data['error'].get('code')}")
    return " ".join(parts)


def render(events: Sequence[Event], *, color: bool = False, ascii: bool = False) -> str:
    """The run as an indented tree, with time offsets from its first event.

    ``ascii`` draws the tree with plain characters, for consoles that can't show box drawing.
    """
    tee, elbow, pipe = ("|- ", "`- ", "|  ") if ascii else ("├─ ", "└─ ", "│  ")
    if not events:
        return "(no events)"
    origin: datetime = events[0].time
    lines: list[str] = []

    def visit(node: Node, prefix: str, last: bool, root: bool) -> None:
        event = node.event
        offset = (event.time - origin).total_seconds()
        branch = "" if root else (elbow if last else tee)
        name = event.type
        if color and event.type.endswith((".failed", ".exceeded", ".cancelled")):
            name = f"\x1b[31m{name}\x1b[0m"
        lines.append(f"{offset:8.3f}s {prefix}{branch}{name} {_summary(event)}".rstrip())
        extension = "" if root else ("   " if last else pipe)
        for index, child in enumerate(node.children):
            visit(child, prefix + extension, index == len(node.children) - 1, False)

    for root in build(events):
        visit(root, "", True, True)
    return "\n".join(lines)
