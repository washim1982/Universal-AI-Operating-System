"""Two runs side by side (FR-OBS-04): every step of either, and where they differ."""

from datetime import UTC, datetime

from aios.observability.compare import compare
from aios.observability.trace import ToolCall, Trace, TraceNode

AT = datetime(2026, 11, 2, 10, tzinfo=UTC)


def _trace(run: str, *nodes: TraceNode) -> Trace:
    return Trace(run_id=run, app="shop", status="completed", nodes=list(nodes))


def test_every_step_of_either_run_is_shown() -> None:
    a = _trace("A", TraceNode(id="check", type="task", status="completed", output=1))
    b = _trace(
        "B",
        TraceNode(id="check", type="task", status="completed", output=2, copied=True),
        TraceNode(id="extra", type="task", status="completed", output=3),
    )
    found = compare(a, b, ("x", "x"))
    assert [step.step for step in found.steps] == ["check", "extra"]
    check, extra = found.steps
    assert not check.same_output
    assert check.same_status
    assert check.b.copied
    assert not check.a.copied
    assert extra.a.status == "not run"
    assert not extra.same_status
    assert found.same_output


def test_held_back_calls_are_counted() -> None:
    held = ToolCall(tool="shop.order.place", status="completed", stubbed=True, at=AT)
    real = ToolCall(tool="shop.stock.read", status="completed", at=AT)
    node = TraceNode(id="pay", type="task", status="completed", tool_calls=[held, real, held])
    found = compare(_trace("A"), _trace("B", node), ("x", "y"))
    assert found.steps[0].b.stubbed == 2
    assert not found.same_output
