"""Physical subjects: how the fabric namespaces logical topics by tenant and app (FR-EVT-04).

``<tenant>.<app>.<logical topic>[.<routing suffix>]``, e.g.
``acme.ai-researcher.task.requested.research.web``. Agent code never sees these: subscribers
pass a :class:`~aios.fabric.port.Scope` and a logical pattern, and the fabric prefixes it. A
tenant-scoped subscription can therefore never match another tenant's events.
"""

from __future__ import annotations

from aios.core import catalog
from aios.core.envelope import Event
from aios.core.errors import InvalidEventError, InvalidTopicError
from aios.core.topics import validate_name, validate_pattern


def routing_subject(event: Event) -> str:
    """The logical topic plus the routing suffix the catalog defines for this event type."""
    spec = catalog.get(event.type)
    if spec is None or spec.route_by is None:
        return event.type
    value = event.data.get(spec.route_by) if isinstance(event.data, dict) else None
    if not isinstance(value, str):
        raise InvalidEventError(f"{event.type} events need a string data.{spec.route_by}")
    try:
        validate_name(value, spec.route_by)
    except InvalidTopicError as exc:
        raise InvalidEventError(str(exc)) from exc
    return f"{event.type}.{value}"


def physical_subject(event: Event) -> str:
    return f"{event.tenantid}.{event.appid}.{routing_subject(event)}"


def physical_pattern(tenant: str, app: str, pattern: str) -> str:
    """Prefix a logical pattern with a scope. ``"*"`` for tenant or app matches any one id."""
    return f"{tenant}.{app}.{validate_pattern(pattern)}"
