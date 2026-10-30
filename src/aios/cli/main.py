"""Entry point for the ``aios`` command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from aios import __version__
from aios.core.schemas import stale_specs, write_specs
from aios.security.cedar_schema import schema_text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aios", description="AIOS command-line interface")
    parser.add_argument("--version", action="version", version=f"aios {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    specs = commands.add_parser("specs", help="generated JSON Schemas and the event catalog")
    actions = specs.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("export", "regenerate the files under the specs directory"),
        ("check", "fail if the generated files are missing or stale"),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--dir", type=Path, default=Path("specs"), help="default: ./specs")
    return parser


def generated_specs() -> dict[str, str]:
    """Spec files generated outside ``aios.core``, which can't import the layers above it."""
    return {"policy/aios.cedarschema": schema_text()}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "specs":
        root: Path = args.dir
        if args.action == "export":
            for path in write_specs(root, generated_specs()):
                print(f"{'wrote' if path.exists() else 'removed'} {path.as_posix()}")
            return 0
        stale = stale_specs(root, generated_specs())
        for path in stale:
            print(f"stale: {path.as_posix()}", file=sys.stderr)
        if stale:
            print("run `aios specs export` and commit the result", file=sys.stderr)
        return 1 if stale else 0
    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
