"""What a replay, fork or retry changes for the components a task calls (ADR-021).

A task carries its run's source (``task.requested`` ``source``), so a worker anywhere knows it
without asking: the Model Kernel answers from the record in a replay, and the Tool OS, the Memory
OS and ``ctx.emit`` hold side effects back in a replay or a fork that isn't live.
"""

from __future__ import annotations

import json
from typing import cast

from aios.core.catalog import ErrorInfo, TaskSource
from aios.core.envelope import Event
from aios.core.errors import AIOSError, RecordedError

_RECORDED: dict[str, type[RecordedError]] = {}


def task_source(event: Event) -> TaskSource | None:
    """The source of the run a task belongs to, if it was replayed, forked or retried."""
    data = event.data if isinstance(event.data, dict) else {}
    found = data.get("source") if event.type == "task.requested" else None
    return TaskSource.model_validate(found) if isinstance(found, dict) else None


def replaying(event: Event) -> TaskSource | None:
    source = task_source(event)
    return source if source is not None and source.mode == "replay" else None


def holding_back(event: Event) -> TaskSource | None:
    """The source, if side effects must not happen: a replay, or a fork that isn't live."""
    source = task_source(event)
    return source if source is not None and not source.live and source.mode != "retry" else None


def as_source[T](value: T, run: str | None, source: str) -> T:
    """``value`` as the source run would have written it: with its run id where this run has its
    own. A replay is a new run, so the one thing allowed to differ from the record is the run id
    an agent put in a path or an idempotency key (``ctx.step_key``)."""
    if not run or run == source:
        return value
    text = json.dumps(value, sort_keys=True, default=str)
    return cast(T, json.loads(text.replace(run, source)))


def as_recorded[T](value: T) -> T:
    """``value`` as any event store gives it back: JSON, with every object's keys sorted.

    PostgreSQL's ``jsonb`` doesn't keep the order of keys. An agent handed a tool's output live,
    and the same output from the record in a replay, must see the same thing, or a prompt built
    from it differs and the replay can't answer it.
    """
    return cast(T, json.loads(json.dumps(value, sort_keys=True, default=str)))


def recorded_error(info: ErrorInfo) -> AIOSError:
    """The failure a replay meets where its source met it, with the same code.

    The engine decides whether to try a step again by its code, so a replay that met a different
    one could take a different path.
    """
    kind = _RECORDED.get(info.code)
    if kind is None:
        kind = _RECORDED[info.code] = type("RecordedError", (RecordedError,), {"code": info.code})
    return kind(info.message, retryable=info.retryable)
