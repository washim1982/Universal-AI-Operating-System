"""Test helpers for AIOS components and third-party adapters.

``aios.testing.fabric`` holds the EventBus conformance suite (requires the ``testing`` extra).
"""

from __future__ import annotations

from typing import Any

from aios.core.envelope import Event
from aios.core.ids import new_ulid

DEFAULT_TENANT = "acme"
DEFAULT_APP = "ai-researcher"


def make_event(
    type: str = "test.thing.happened",
    data: Any = None,
    *,
    tenant: str = DEFAULT_TENANT,
    app: str = DEFAULT_APP,
    run_id: str | None = None,
    **fields: Any,
) -> Event:
    """Build a valid event with sensible defaults; ``fields`` override any envelope attribute."""
    attributes: dict[str, Any] = {
        "source": f"aios://{tenant}/apps/{app}/agents/test-agent",
        "type": type,
        "tenantid": tenant,
        "appid": app,
        "runid": run_id,
        "correlationid": run_id,
        "principal": f"agent:test-agent@{tenant}",
        "data": data,
        **fields,
    }
    return Event.model_validate(attributes)


def task_request(capability: str, input: Any = None, **fields: Any) -> Event:
    """A ``task.requested`` command for ``capability``."""
    return make_event(
        "task.requested",
        {"capability": capability, "input": input},
        run_id=fields.pop("run_id", None) or new_ulid(),
        **fields,
    )
