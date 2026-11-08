"""Bundles: an App directory as one immutable, reproducible thing (ADR-015).

The same directory always gives the same digest, whenever and wherever it is packed, so a digest
names exactly what was reviewed. Code unpacks into a folder named after that digest, so two versions
never overwrite each other.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import jsonschema
import yaml

from aios.core.apps import Bundle, Requested
from aios.core.envelope import DataClass
from aios.core.errors import AIOSError
from aios.core.manifests import (
    AgentManifest,
    AnyManifest,
    AppManifest,
    Entrypoint,
    ScheduleEntrypoint,
    ToolStep,
    WebhookEntrypoint,
    WorkflowManifest,
    load_manifests,
    manifest_files,
)
from aios.core.memory import MemoryScope


class BundleError(AIOSError):
    code = "app.invalid"


def _digest(name: str, version: str, documents: str, schema: Any, code: str | None) -> str:
    canonical = json.dumps(
        [name, version, documents, schema, code], sort_keys=True, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def verified(bundle: Bundle) -> Bundle:
    """Refuse a bundle that isn't what its digest says."""
    expected = _digest(
        bundle.name, bundle.version, bundle.documents, bundle.config_schema, bundle.code
    )
    if expected != bundle.digest:
        raise BundleError(f"{bundle.name} {bundle.version}: its digest doesn't match its contents")
    return bundle


def _packed(source: Path) -> str:
    """A folder as a .tar.gz that depends on nothing but the files' names and contents."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path in sorted(source.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            info = tarfile.TarInfo(path.relative_to(source).as_posix())
            content = path.read_bytes()
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return base64.b64encode(gzip.compress(raw.getvalue(), mtime=0)).decode()


def _as_yaml(file: Path) -> str:
    """A manifest file as YAML documents: a JSON one becomes YAML, so a bundle is one stream."""
    text = file.read_text("utf-8").strip()
    if file.suffix != ".json":
        return text
    found = json.loads(text)
    documents = cast(list[Any], found) if isinstance(found, list) else [found]
    return "\n---\n".join(yaml.safe_dump(d, sort_keys=False).strip() for d in documents)


def bundle_of(path: str | Path) -> Bundle:
    """Pack an App directory: ``manifests/``, ``policies/``, ``config.schema.json`` and ``src/``."""
    root = Path(path)
    if not (root / "manifests").is_dir():
        raise BundleError(f"{root} is not an App directory (no manifests/ folder)")
    files = [f for folder in ("manifests", "policies") for f in manifest_files(root / folder)]
    documents = "\n---\n".join(_as_yaml(f) for f in files)
    schema_file = root / "config.schema.json"
    schema = json.loads(schema_file.read_text("utf-8")) if schema_file.exists() else None
    code = _packed(root / "src") if (root / "src").is_dir() else None
    manifests = rendered(documents, schema, None)
    app = _app_in(manifests)
    name, version = app.metadata.name, app.metadata.version
    return Bundle(
        name=name,
        version=version,
        digest=_digest(name, version, documents, schema, code),
        documents=documents,
        config_schema=schema,
        code=code,
    )


def bundle_from_documents(documents: str) -> Bundle:
    """Manifests alone, as ``aios apply`` sends them, as a version."""
    app = check(rendered(documents, None, None))
    name, version = app.metadata.name, app.metadata.version
    digest = _digest(name, version, documents, None, None)
    return Bundle(name=name, version=version, digest=digest, documents=documents)


def filled_in(schema: Mapping[str, Any] | None, config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The configuration an install runs with, checked against the App's schema (FR-APP-07)."""
    properties = (schema or {}).get("properties", {})
    defaults = {k: v["default"] for k, v in properties.items() if "default" in v}
    values = {**defaults, **(config or {})}
    if schema is not None:
        try:
            jsonschema.validate(values, dict(schema))
        except jsonschema.ValidationError as exc:
            raise BundleError(f"invalid configuration: {exc.message}") from exc
    return values


def rendered(
    documents: str, schema: Mapping[str, Any] | None, config: Mapping[str, Any] | None
) -> list[AnyManifest]:
    try:
        return load_manifests(documents, config=filled_in(schema, config))
    except ValueError as exc:
        raise BundleError(str(exc)) from exc


def _app_in(manifests: list[AnyManifest]) -> AppManifest:
    apps = [m for m in manifests if isinstance(m, AppManifest)]
    if len(apps) != 1:
        raise BundleError(f"expected exactly one App manifest, found {len(apps)}")
    return apps[0]


def check(manifests: list[AnyManifest]) -> AppManifest:
    """Refuse an App that couldn't start, while someone is still there to hear about it."""
    app = _app_in(manifests)
    agents = {m.metadata.name for m in manifests if isinstance(m, AgentManifest)}
    workflows = {m.metadata.name for m in manifests if isinstance(m, WorkflowManifest)}
    missing = [
        *(f"Agent {name}" for name in app.spec.agents if name not in agents),
        *(f"Workflow {name}" for name in app.spec.workflows if name not in workflows),
    ]
    if missing:
        raise BundleError(f"App {app.metadata.name}: no manifest for {', '.join(missing)}")
    return app


def requested_by(manifests: list[AnyManifest]) -> Requested:
    """Everything an App would be able to do, for a person to agree to (FR-APP-03).

    A manifest can't ask quietly: what its agents are allowed counts, whether or not the App's
    ``permissions`` section mentions it.
    """
    app = check(manifests)
    agents = [m for m in manifests if isinstance(m, AgentManifest)]
    steps = [
        s.tool
        for m in manifests
        if isinstance(m, WorkflowManifest)
        for s in m.spec.steps
        if isinstance(s, ToolStep)
    ]
    tools = {
        *app.spec.permissions.tools,
        *(t for a in agents for t in a.spec.tools.allow),
        *steps,
    }
    scopes: set[MemoryScope] = {*app.spec.memory.scopes}
    for agent in agents:
        scopes |= {*agent.spec.memory.read, *agent.spec.memory.write}
    classes: list[DataClass] = sorted(app.spec.permissions.data_classes)
    return Requested(
        tools=sorted(tools),
        data_classes=classes,
        memory_scopes=sorted(scopes),
        hosts=sorted({*(app.spec.egress.allow or []), *_tool_hosts(app)}),
        per_run_usd=app.spec.budgets.per_run_usd,
        monthly_usd=app.spec.budgets.monthly_usd,
        starts=[_starts(entry) for entry in app.spec.entrypoints],
    )


def _starts(entry: Entrypoint) -> str:
    if isinstance(entry, ScheduleEntrypoint):
        return f"schedule: {entry.cron}"
    if isinstance(entry, WebhookEntrypoint):
        return f"webhook: {entry.name}"
    return entry.type


def _tool_hosts(app: AppManifest) -> set[str]:
    return {host for hosts in app.spec.egress.tools.values() for host in hosts}


def code_folder(bundle: Bundle, cache: Path) -> Path | None:
    """Unpack a bundle's code once, and make it importable. Returns where it went."""
    if bundle.code is None:
        return None
    target = cache / bundle.digest.removeprefix("sha256:")
    if not target.is_dir():
        cache.mkdir(parents=True, exist_ok=True)
        partial = Path(tempfile.mkdtemp(dir=cache, prefix=".partial-"))  # workers share ``cache``
        raw = gzip.decompress(base64.b64decode(bundle.code))
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
            tar.extractall(partial, filter="data")  # refuses paths that climb out of the folder
        try:
            partial.rename(target)
        except OSError:
            if not target.is_dir():
                raise
            shutil.rmtree(partial, ignore_errors=True)  # another process got there first
    _import_from(target)
    return target


def _import_from(folder: Path) -> None:
    """Put a folder first, and forget what its packages were: Python would keep the old code."""
    packages = {path.stem for path in folder.iterdir() if not path.name.startswith("__")}
    for module in [name for name in sys.modules if name.split(".")[0] in packages]:
        del sys.modules[module]
    if str(folder) in sys.path:
        sys.path.remove(str(folder))
    sys.path.insert(0, str(folder))
    importlib.invalidate_caches()
