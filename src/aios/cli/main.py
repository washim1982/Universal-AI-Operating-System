"""Entry point for the ``aios`` command (WP 1.7; FR-CP-03).

aios dev --app apps/ai-researcher          # runtime + API in one process
aios run ai-researcher "Your question"     # start a run and follow it
aios trace <run-id> --app ai-researcher    # the run as a causal tree
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from aios import __version__
from aios.core.schemas import stale_specs, write_specs
from aios.security.cedar_schema import schema_text

DEFAULT_SERVER = "http://127.0.0.1:8080"


def generated_specs() -> dict[str, str]:
    """Spec files generated outside ``aios.core``, which can't import the layers above it."""
    return {"policy/aios.cedarschema": schema_text()}


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

    for role, help_text in (
        ("dev", "run the runtime and the API in one process"),
        ("api", "run the API process (needs --database-url)"),
        ("worker", "run a worker process: engine and agents (needs --database-url)"),
    ):
        server = commands.add_parser(role, help=help_text)
        server.add_argument("--app", type=Path, action="append", default=[], help="App directory")
        server.add_argument("--host", default="127.0.0.1")
        server.add_argument("--port", type=int, default=8080)
        server.add_argument("--tenant", default=os.environ.get("AIOS_TENANT", "local"))
        server.add_argument("--database-url", default=os.environ.get("AIOS_DATABASE_URL"))
        server.add_argument("--cassettes", type=Path, default=None, help="record/replay directory")
        server.add_argument(
            "--cassette-mode", choices=["replay", "record", "live"], default="replay"
        )
        server.add_argument("--log-level", default="info")

    def client(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--server", default=os.environ.get("AIOS_SERVER", DEFAULT_SERVER))
        command.add_argument("--tenant", default=os.environ.get("AIOS_TENANT"))
        return command

    run = client("run", "start a run through an App's chat entrypoint and follow it")
    run.add_argument("app")
    run.add_argument("message")
    for name, help_text in (("trace", "show a run as a causal tree"), ("logs", "a run's events")):
        command = client(name, help_text)
        command.add_argument("run_id")
        command.add_argument("--app", required=True)
    apply = client("apply", "install manifests (a YAML file or a directory)")
    apply.add_argument("-f", "--file", type=Path, required=True)
    get = client("get", "list resources")
    get.add_argument("resource", choices=["apps"])
    events = client("events", "the event log")
    events.add_argument("action", choices=["tail"])
    events.add_argument("--app", default=None)
    events.add_argument("--pattern", default=">")
    dlq = client("dlq", "dead letters: list, redrive or discard")
    dlq.add_argument("action", choices=["list", "redrive", "discard"])
    dlq.add_argument("id", nargs="?")

    init = commands.add_parser("init", help="create a new App from a template")
    init.add_argument("name")
    init.add_argument("--dir", type=Path, default=Path())
    return parser


def _specs(args: argparse.Namespace) -> int:
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


def _serve(args: argparse.Namespace) -> int:
    from aios.cli.server import serve  # noqa: PLC0415 - keeps `aios --version` light

    if args.command in ("api", "worker") and not args.database_url:
        raise SystemExit(f"aios {args.command} needs --database-url (or AIOS_DATABASE_URL)")
    try:
        asyncio.run(
            serve(
                args.command,
                apps=args.app,
                host=args.host,
                port=args.port,
                tenant=args.tenant,
                database_url=args.database_url,
                cassettes=args.cassettes,
                cassette_mode=args.cassette_mode,
                log_level=args.log_level,
            )
        )
    except KeyboardInterrupt:
        return 130
    return 0


def _client(args: argparse.Namespace) -> int:  # noqa: PLR0911 - one branch per command
    from aios.cli import client  # noqa: PLC0415

    api = client.Client(args.server, args.tenant)
    if args.command == "run":
        return client.run_and_follow(api, args.app, args.message)
    if args.command == "trace":
        return client.trace(api, args.app, args.run_id)
    if args.command == "logs":
        return client.logs(api, args.app, args.run_id)
    if args.command == "apply":
        print(json.dumps(api.apply(args.file), indent=2))
        return 0
    if args.command == "get":
        print(json.dumps(api.apps(), indent=2))
        return 0
    if args.command == "events":
        return client.tail(api, args.app, args.pattern)
    if args.action == "list":
        print(json.dumps(api.dlq(), indent=2))
        return 0
    if not args.id:
        raise SystemExit(f"aios dlq {args.action} needs a dead-letter id")
    if args.action == "redrive":
        api.redrive(args.id)
    else:
        api.discard(args.id)
    print(f"{args.action}: {args.id}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "specs":
        return _specs(args)
    if args.command in ("dev", "api", "worker"):
        return _serve(args)
    if args.command == "init":
        from aios.cli.scaffold import init  # noqa: PLC0415

        print(f"created {init(args.name, args.dir).as_posix()}")
        return 0
    return _client(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
