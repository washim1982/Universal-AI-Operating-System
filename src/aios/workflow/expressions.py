"""Input bindings and output expressions of the graph IR.

``${input}`` and ``${input.<path>}`` read the run's input; ``${<step>.output}`` and
``${<step>.output.<path>}`` read a step's latest output. A string that is exactly one
expression keeps the value's type; expressions inside a longer string are replaced by text.
``fromPlan`` uses the same paths without the braces (``plan.output.tasks``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, cast

_EXPRESSION = re.compile(r"\$\{([^}]+)\}")


class ExpressionError(ValueError):
    pass


def lookup(path: str, run_input: Any, outputs: Mapping[str, Any]) -> Any:
    """Resolve ``input[.a.b]`` or ``<step>.output[.a.b]``."""
    head, *rest = path.strip().split(".")
    if head == "input":
        value: Any = run_input
    else:
        if not rest or rest[0] != "output":
            raise ExpressionError(f"expected <step>.output[...] in {path!r}")
        if head not in outputs:
            raise ExpressionError(f"step {head!r} has no output yet ({path!r})")
        value, rest = outputs[head], rest[1:]
    for key in rest:
        if isinstance(value, Mapping) and key in value:
            value = cast(Mapping[str, Any], value)[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(cast(list[Any], value)):
            value = cast(list[Any], value)[int(key)]
        else:
            raise ExpressionError(f"{path!r}: no {key!r}")
    return value


def resolve(template: Any, run_input: Any, outputs: Mapping[str, Any]) -> Any:
    """Resolve every expression in a binding (dicts and lists are walked)."""
    if isinstance(template, dict):
        return {
            k: resolve(v, run_input, outputs) for k, v in cast(dict[str, Any], template).items()
        }
    if isinstance(template, list):
        return [resolve(v, run_input, outputs) for v in cast(list[Any], template)]
    if not isinstance(template, str):
        return template
    whole = _EXPRESSION.fullmatch(template.strip())
    if whole is not None:
        return lookup(whole[1], run_input, outputs)

    def text(match: re.Match[str]) -> str:
        value = lookup(match[1], run_input, outputs)
        return value if isinstance(value, str) else json.dumps(value)

    return _EXPRESSION.sub(text, template)
