"""Whether a schema can replace another without breaking anyone (FR-EVT-10; WP 3.6).

Events are kept for good, and every component reads stored events with today's models: a fold, a
replay, an audit. So a change to an event's schema must still accept every event written under the
old one. Manifests are written by people, and today's AIOS must still read yesterday's.

Breaking, unless the version goes up (a new ``.v2`` schema):

* a field removed, or no longer allowed;
* a field that becomes required (old documents may not have it);
* a field whose type narrows (``string`` or ``null`` becomes ``string``);
* an enumerated value removed;
* a type of event, or kind of manifest, removed.

Adding an optional field, a new value, a new type or a new kind breaks nobody.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

Schema = Mapping[str, Any]


def breaking_changes(old: Schema, new: Schema, where: str = "") -> list[str]:
    """What in ``new`` would refuse documents ``old`` accepted."""
    found = _object(old, new, old, new, where)
    old_defs = cast(Mapping[str, Schema], old.get("$defs") or {})
    new_defs = cast(Mapping[str, Schema], new.get("$defs") or {})
    for name in sorted(set(old_defs) & set(new_defs)):
        found += _object(old_defs[name], new_defs[name], old, new, f"{where}{name}.")
    return found


def _object(old: Schema, new: Schema, old_root: Schema, new_root: Schema, where: str) -> list[str]:
    found: list[str] = []
    old_props = cast(Mapping[str, Schema], old.get("properties") or {})
    new_props = cast(Mapping[str, Schema], new.get("properties") or {})
    for name in sorted(old_props):
        if name not in new_props:
            if new.get("additionalProperties") is False or not new_props:
                found.append(f"{where}{name}: removed")
            continue
        found += _field(old_props[name], new_props[name], old_root, new_root, f"{where}{name}")
    newly_required = set(new.get("required") or ()) - set(old.get("required") or ())
    found += [f"{where}{name}: now required" for name in sorted(newly_required)]
    if new.get("additionalProperties") is False and old.get("additionalProperties") is not False:
        found.append(f"{where.rstrip('.') or 'the document'}: no longer takes other fields")
    return found


def _field(old: Schema, new: Schema, old_root: Schema, new_root: Schema, where: str) -> list[str]:
    found: list[str] = []
    before, after = _types(old, old_root), _types(new, new_root)
    if "number" in after:
        before.discard("integer")  # every integer is a number
    if before and after and not before <= after:
        lost = ", ".join(sorted(before - after))
        found.append(f"{where}: no longer takes {lost}")
    old_values, new_values = _values(old), _values(new)
    if old_values is not None and new_values is not None:
        gone = [v for v in old_values if v not in new_values]
        if gone:
            found.append(f"{where}: no longer takes {', '.join(json.dumps(v) for v in gone)}")
    return found


def _types(schema: Schema, root: Schema) -> set[str]:
    """What kinds of value a field takes. ``{}`` (anything) gives the empty set: unknown."""
    if "$ref" in schema:
        return {f"a {str(schema['$ref']).rsplit('/', 1)[-1]}"}
    members = cast(list[Schema], schema.get("anyOf") or schema.get("oneOf") or [])
    if members:
        return set[str]().union(*(_types(member, root) for member in members))
    kind = schema.get("type")
    if isinstance(kind, list):
        return {str(k) for k in cast(list[Any], kind)}
    if isinstance(kind, str):
        return {kind}
    return set()


def _values(schema: Schema) -> list[Any] | None:
    if "enum" in schema:
        return list(cast(list[Any], schema["enum"]))
    if "const" in schema:
        return [schema["const"]]
    members = cast(list[Schema], schema.get("anyOf") or schema.get("oneOf") or [])
    if not members:
        return None
    values: list[Any] = []
    for member in members:
        listed = _values(member)
        if listed is not None:
            values += listed
        elif member.get("type") == "null":
            values.append(None)
        else:
            return None  # it takes any value of some type
    return values


def compare_folders(old: Path, new: Mapping[str, str]) -> list[str]:
    """Breaking changes from the specs under ``old`` (``specs/``, as committed on the base branch)
    to ``new``, the files generated now, keyed by their path under ``specs/``."""
    found: list[str] = []
    for path in sorted((old / "schemas").rglob("*.json")):
        relative = path.relative_to(old).as_posix()
        label = relative.removeprefix("schemas/").removesuffix(".json")
        if relative not in new:
            found.append(f"{label}: removed")
            continue
        before = cast(Schema, json.loads(path.read_text(encoding="utf-8")))
        after = cast(Schema, json.loads(new[relative]))
        found += [f"{label}: {change}" for change in breaking_changes(before, after)]
    return found
