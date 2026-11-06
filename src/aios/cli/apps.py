"""``aios app``: package an App, keep it in a registry, and look after it in a tenant (WP 3.1)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

from aios.cli.client import Client
from aios.cli.oci import Reference, Registry
from aios.controlplane.bundle import BundleError, bundle_of, verified
from aios.core.apps import Bundle


def _source(source: str) -> Bundle:
    """A bundle from a directory, a saved file, or a registry."""
    path = Path(source)
    if path.is_dir():
        return bundle_of(path)
    if path.is_file():
        return verified(Bundle.model_validate_json(path.read_text("utf-8-sig")))
    return Registry(Reference.parse(source)).pull()


def _save(bundle: Bundle, output: Path | None) -> dict[str, str]:
    target = output or Path(f"{bundle.name}-{bundle.version}.json")
    target.write_text(bundle.model_dump_json(), "utf-8")
    return {"file": str(target), "digest": bundle.digest}


def _push(source: str, reference: str | None) -> dict[str, str]:
    if reference is None:
        raise SystemExit("aios app push: where to? Try: aios app push DIR registry/name:tag")
    bundle = _source(source)
    manifest = Registry(Reference.parse(reference)).push(bundle)
    return {"pushed": reference, "manifest": manifest, "digest": bundle.digest}


def _config(path: Path | None) -> dict[str, Any] | None:
    """A JSON file. PowerShell 5.1 writes UTF-8 with a byte-order mark, so that is allowed."""
    return None if path is None else json.loads(path.read_text("utf-8-sig"))


def _install(api: Client, args: argparse.Namespace) -> Any:
    found = api.install(_source(args.target), _config(args.config))
    return api.consent(found["app"]) if args.yes and found.get("pending") else found


def _consent(api: Client, args: argparse.Namespace) -> Any:
    """Agree to what's waiting, less what's left out and what was refused before."""
    denied = {
        "tools": args.deny_tool,
        "data_classes": args.deny_class,
        "memory_scopes": args.deny_scope,
        "hosts": args.deny_host,
    }
    if not args.all and not any(denied.values()):
        return api.consent(args.target)
    shown = api.installation(args.target)
    wanted = shown.get("requested")
    if wanted is None:
        raise SystemExit(f"{args.target} isn't waiting for anyone's consent")
    earlier: dict[str, list[str]] = {} if args.all else shown.get("refused", {})
    return api.consent(
        args.target,
        {
            field: [v for v in wanted[field] if v not in left and v not in earlier.get(field, ())]
            for field, left in denied.items()
        },
    )


def _rollback(api: Client, args: argparse.Namespace) -> Any:
    if args.to is None:
        raise SystemExit("aios app rollback: to which version?")
    return api.rollback(args.target, args.to)


def _export(api: Client, app: str, output: Path | None) -> dict[str, Any]:
    target = output or Path(f"{app}-export.jsonl")
    return {"file": str(target), "lines": api.export(app, target)}


def _uninstall(api: Client, args: argparse.Namespace) -> Any:
    """``export`` saves everything first, then deletes it: the data leaves with you."""
    if args.data is None:
        raise SystemExit(
            "aios app uninstall: what happens to its data? --data retain|export|delete"
        )
    if args.data == "export":
        print(json.dumps(_export(api, args.target, args.output)))
    return api.uninstall(args.target, "retain" if args.data == "retain" else "delete")


def run(api: Client, args: argparse.Namespace) -> int:
    target: str = args.target or ""
    if not target and args.action != "list":
        raise SystemExit(f"aios app {args.action}: of which App?")
    actions: dict[str, Callable[[], Any]] = {
        "package": lambda: _save(bundle_of(Path(target)), args.output),
        "push": lambda: _push(target, args.to),
        "pull": lambda: _save(Registry(Reference.parse(target)).pull(), args.output),
        "install": lambda: _install(api, args),
        "list": api.installations,
        "show": lambda: api.installation(target),
        "consent": lambda: _consent(api, args),
        "refuse": lambda: api.refuse(target),
        "rollback": lambda: _rollback(api, args),
        "configure": lambda: api.configure(target, _config(args.config) or {}),
        "export": lambda: _export(api, target, args.output),
        "uninstall": lambda: _uninstall(api, args),
    }
    try:
        print(json.dumps(actions[args.action](), indent=2))
    except (BundleError, ValueError, httpx2.HTTPError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    return 0
