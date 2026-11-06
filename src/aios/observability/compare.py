"""Two runs side by side, step by step (FR-OBS-04): usually a run and its replay or fork."""

from __future__ import annotations

from pydantic import BaseModel, Field, JsonValue

from aios.observability.trace import Trace, TraceNode


class Side(BaseModel):
    status: str = "not run"
    output: JsonValue = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float | None = None
    copied: bool = Field(default=False, description="Kept from its source, not run")
    stubbed: int = Field(default=0, description="Side effects held back")
    error: str | None = None


class StepComparison(BaseModel):
    step: str
    a: Side
    b: Side
    same_output: bool
    same_status: bool


class Comparison(BaseModel):
    a: str
    b: str
    status: tuple[str, str]
    output: tuple[JsonValue, JsonValue]
    same_output: bool
    cost_usd: tuple[float, float]
    steps: list[StepComparison]


def _side(node: TraceNode | None) -> Side:
    if node is None:
        return Side()
    return Side(
        status=node.status,
        output=node.output,
        cost_usd=node.cost_usd,
        input_tokens=node.input_tokens,
        output_tokens=node.output_tokens,
        duration_ms=node.duration_ms,
        copied=node.copied,
        stubbed=sum(call.stubbed for call in node.tool_calls),
        error=node.error,
    )


def compare(a: Trace, b: Trace, outputs: tuple[JsonValue, JsonValue]) -> Comparison:
    """Every step of either run: the first run's in its order, then any only the second has."""
    nodes_a = {node.id: node for node in a.nodes}
    nodes_b = {node.id: node for node in b.nodes}
    order = list(nodes_a) + [step for step in nodes_b if step not in nodes_a]
    steps: list[StepComparison] = []
    for step in order:
        side_a, side_b = _side(nodes_a.get(step)), _side(nodes_b.get(step))
        steps.append(
            StepComparison(
                step=step,
                a=side_a,
                b=side_b,
                same_output=side_a.output == side_b.output,
                same_status=side_a.status == side_b.status,
            )
        )
    return Comparison(
        a=a.run_id,
        b=b.run_id,
        status=(a.status, b.status),
        output=outputs,
        same_output=outputs[0] == outputs[1],
        cost_usd=(a.cost_usd, b.cost_usd),
        steps=steps,
    )
