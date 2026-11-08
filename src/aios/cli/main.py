"""Entry point for the ``aios`` command (WP 1.7; FR-CP-03).

aios dev --app apps/ai-researcher          # runtime + API in one process
aios run ai-researcher "Your question"     # start a run and follow it
aios replay <run-id> --app ai-researcher  # again, from its record; also fork, retry, compare
aios trace <run-id> --app ai-researcher    # the run as a causal tree
aios pause <run-id> --app ai-researcher    # hold a run; also resume, cancel, status, signal
aios audit verify                          # check the tenant's audit chain
aios suspend agent refund-agent --reason … # the kill switch
aios memory ingest ./kb --app support-desk # bring documents into an App's memory
aios approvals list                        # what you can decide; also show, approve, deny
aios app install ./desk                    # offer an App; also consent, rollback, uninstall…
aios platform key --tenant acme --name ops # a tenant's first key, with database access
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aios import __version__
from aios.core.schemas import stale_specs, write_specs
from aios.security.cedar_schema import schema_text

if TYPE_CHECKING:
    from aios.cli.client import Client

DEFAULT_SERVER = "http://127.0.0.1:8080"
DEFAULT_TENANT = "local"
BUILTIN_ROLES = ("member", "auditor", "operator", "tenant-admin", "platform-admin")


APP_ACTIONS = (
    "package", "push", "pull", "install", "list", "show", "consent", "refuse", "rollback",
    "configure", "export", "uninstall",
)  # fmt: skip


def _changes(args: argparse.Namespace) -> dict[str, Any]:
    """What a fork or retry changes. JSON is read as JSON; anything else is taken as text."""

    def value(text: str) -> Any:
        try:
            return json.loads(text)
        except ValueError:
            return text

    body: dict[str, Any] = {"step": args.step, "model": args.model}
    if args.input is not None:
        body["input"] = value(args.input)
    if args.step_input is not None:
        body["step_input"] = value(args.step_input)
    return {key: found for key, found in body.items() if found is not None}


def generated_specs() -> dict[str, str]:
    """Spec files generated outside ``aios.core``, which can't import the layers above it."""
    return {"policy/aios.cedarschema": schema_text()}


def _parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - the command table
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
    compat = actions.add_parser(
        "compat", help="fail if the schemas now would refuse events or manifests the old ones took"
    )
    compat.add_argument(
        "--against", type=Path, required=True, help="the old specs/ folder, e.g. the base branch's"
    )

    for role, help_text in (
        ("dev", "run the runtime and the API in one process"),
        ("api", "run the API process (needs --database-url)"),
        ("worker", "run a worker process: engine and agents (needs --database-url)"),
    ):
        server = commands.add_parser(role, help=help_text)
        server.add_argument("--app", type=Path, action="append", default=[], help="App directory")
        server.add_argument("--host", default="127.0.0.1")
        server.add_argument("--port", type=int, default=8080)
        server.add_argument(
            "--tenant",
            action="append",
            default=None,
            help="install the Apps for this tenant; repeat for more (default: AIOS_TENANT, local)",
        )
        server.add_argument("--database-url", default=os.environ.get("AIOS_DATABASE_URL"))
        server.add_argument("--cassettes", type=Path, default=None, help="record/replay directory")
        server.add_argument(
            "--cassette-mode", choices=["replay", "record", "live"], default="replay"
        )
        server.add_argument("--log-level", default="info")
        server.add_argument(
            "--no-auth", action="store_true", help="aios api: no sign-in, on a loopback address"
        )
        server.add_argument("--tls-cert", type=Path, default=None, help="PEM certificate chain")
        server.add_argument("--tls-key", type=Path, default=None, help="PEM private key")

    def client(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--server", default=os.environ.get("AIOS_SERVER", DEFAULT_SERVER))
        command.add_argument("--tenant", default=os.environ.get("AIOS_TENANT"))
        return command

    run = client("run", "start a run through an App's chat entrypoint and follow it")
    run.add_argument("app")
    run.add_argument("message")
    for name, help_text in (
        ("trace", "show a run as a causal tree"),
        ("logs", "a run's events"),
        ("status", "a run's status, its steps and what it waits for"),
        ("pause", "hold a run: it starts no new work until resumed"),
        ("resume", "continue a held run"),
        ("cancel", "cancel a run"),
    ):
        command = client(name, help_text)
        command.add_argument("run_id")
        command.add_argument("--app", required=True)
    replay = client("replay", "run a finished run again, answered from its record")
    replay.add_argument("run_id")
    replay.add_argument("--app", required=True)
    for name, help_text in (
        ("fork", "start a finished run again from a step, side effects held back"),
        ("retry", "start a finished run again from a step, for real"),
    ):
        command = client(name, help_text)
        command.add_argument("run_id")
        command.add_argument("--app", required=True)
        command.add_argument("--from", dest="step", required=name == "fork", help="the step")
        command.add_argument("--input", default=None, help="a new input for the run, as JSON")
        command.add_argument("--step-input", default=None, help="a new input for that step")
        command.add_argument("--model", default=None, help="a registry entry every call uses")
    compare = client("compare", "two runs side by side, step by step")
    compare.add_argument("run_id")
    compare.add_argument("other")
    compare.add_argument("--app", required=True)
    signal = client("signal", "send a signal to a run, for a wait step")
    signal.add_argument("run_id")
    signal.add_argument("name")
    signal.add_argument("--app", required=True)
    signal.add_argument("--data", default=None, help="JSON, or plain text")
    apply = client("apply", "offer manifests to the server (a YAML file or a directory)")
    apply.add_argument("-f", "--file", type=Path, required=True)
    apply.add_argument("--yes", action="store_true", help="agree to all it asks for")
    app = client("app", "package, install and look after an App")
    app.add_argument("action", choices=APP_ACTIONS)
    app.add_argument("target", nargs="?", help="an App's name, directory or OCI reference")
    app.add_argument("to", nargs="?", help="push: the reference; rollback: the version")
    app.add_argument("--config", type=Path, help="install, configure: a JSON file")
    app.add_argument("--yes", action="store_true", help="install: agree to all it asks for")
    app.add_argument(
        "--all", action="store_true", help="consent: all of it, even what was refused before"
    )
    app.add_argument("--deny-tool", action="append", default=[], help="consent: leave this out")
    app.add_argument("--deny-scope", action="append", default=[], help="consent: leave this out")
    app.add_argument("--deny-class", action="append", default=[], help="consent: leave this out")
    app.add_argument("--deny-host", action="append", default=[], help="consent: leave this out")
    app.add_argument("--data", choices=["retain", "export", "delete"], help="uninstall")
    app.add_argument("-o", "--output", type=Path, help="package, pull, export: where to save it")
    get = client("get", "list resources")
    get.add_argument("resource", choices=["apps"])
    events = client("events", "the event log")
    events.add_argument("action", choices=["tail"])
    events.add_argument("--app", default=None)
    events.add_argument("--pattern", default=">")
    dlq = client("dlq", "dead letters: list, redrive or discard")
    dlq.add_argument("action", choices=["list", "redrive", "discard"])
    dlq.add_argument("id", nargs="?")
    audit = client("audit", "the tenant's audit log: check its chain, or list entries")
    audit.add_argument("action", choices=["verify", "list"])
    audit.add_argument("--after", type=int, default=0)
    audit.add_argument("--limit", type=int, default=100)
    keys = client("keys", "API keys of the caller's tenant")
    keys.add_argument("action", choices=["list", "create", "revoke"])
    keys.add_argument("name", nargs="?", help="create: the key's name; revoke: its id")
    keys.add_argument("--role", action="append", default=[], help="repeat for more roles")
    keys.add_argument("--expires-in-days", type=int, default=None)
    suspend = client("suspend", "kill switch: stop an agent, an App or the whole tenant")
    suspend.add_argument("kind", choices=["agent", "app", "tenant"])
    suspend.add_argument("name")
    suspend.add_argument("--reason", required=True)
    unsuspend = client("unsuspend", "end a suspension")
    unsuspend.add_argument("kind", choices=["agent", "app", "tenant"])
    unsuspend.add_argument("name")
    client("suspensions", "what is suspended in the caller's tenant")
    memory = client("memory", "search, read and erase memory, and ingest documents")
    memory.add_argument("action", choices=["search", "get", "forget", "ingest"])
    memory.add_argument(
        "target", nargs="?", help="search: a query; get: an id; ingest: path or URL"
    )
    memory.add_argument("--app", help="ingest: the App the documents belong to")
    memory.add_argument("--scope", action="append", default=None, help="repeat for more scopes")
    memory.add_argument("-k", type=int, default=10)
    memory.add_argument("--subject", help="forget: everything about this principal")
    memory.add_argument("--item", help="forget: this item")
    memory.add_argument("--sensitivity", default="internal")
    memory.add_argument(
        "--keep", action="store_true", help="ingest: don't remove documents that have gone"
    )

    approvals = client("approvals", "what people are asked to decide: list, show, answer")
    approvals.add_argument("action", choices=["list", "show", "approve", "deny"])
    approvals.add_argument("id", nargs="?")
    approvals.add_argument("--comment", help="approve, deny: recorded with the decision")
    approvals.add_argument("--status", default="pending", help="list: with --all, this status")
    approvals.add_argument("--all", action="store_true", help="list: everyone's, for auditors")
    approvals.add_argument(
        "--edit",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="approve: change a parameter first (JSON, or text); repeat for more",
    )

    feedback = client("feedback", "what people said about runs: give, list, cases")
    feedback.add_argument("action", choices=["give", "list", "cases"])
    feedback.add_argument("app")
    feedback.add_argument("run_id", nargs="?", help="give: the run; list: only this run")
    feedback.add_argument("--rating", choices=["good", "bad"], help="give")
    feedback.add_argument("--step", help="give: about this step, not the whole run")
    feedback.add_argument("--correction", help="give: a right result, as JSON (or text)")
    feedback.add_argument(
        "--correct",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="give: the right result one field at a time (JSON, or text); repeat for more",
    )
    feedback.add_argument("--comment", help="give")
    feedback.add_argument("--remember", action="store_true", help="give: keep it in memory too")

    describe = client("describe", "one thing, in words: app, run, agent, approval, task")
    describe.add_argument("kind", choices=["app", "run", "agent", "approval", "task"])
    describe.add_argument("name", help="its name or id")
    describe.add_argument("--app", help="run: its App; agent: if two Apps have one by that name")

    tasks = client("tasks", "work assigned to people: list, show, complete")
    tasks.add_argument("action", choices=["list", "show", "complete"])
    tasks.add_argument("id", nargs="?")
    tasks.add_argument("--output", help="complete: the result, as JSON (or text)")
    tasks.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="complete: the result one field at a time (JSON, or text); repeat for more",
    )
    tasks.add_argument("--comment", help="complete: recorded with the work")
    tasks.add_argument("--all", action="store_true", help="list: everyone's, for operators")

    groups = client("groups", "the tenant's groups, which approvals can name")
    groups.add_argument("action", choices=["list", "set"])
    groups.add_argument("name", nargs="?", help="set: the group")
    groups.add_argument("--member", action="append", default=[], help="set: repeat for each")

    evaluation = client("eval", "evaluation suites: run one, compare, grade by hand")
    evaluation.add_argument("action", choices=["run", "list", "show", "compare", "grade", "online"])
    evaluation.add_argument("target", nargs="?", help="run: the suite's YAML; else an experiment")
    evaluation.add_argument("other", nargs="?", help="compare: the second experiment")
    evaluation.add_argument("--app", help="the App; run takes it from the suite if not given")
    evaluation.add_argument(
        "--app-dir", type=Path, help="run: here, in this process, with the App in this folder"
    )
    evaluation.add_argument("--cassettes", type=Path, help="run --app-dir: recorded answers")
    evaluation.add_argument(
        "--cassette-mode", choices=["replay", "record", "live"], default="replay"
    )
    evaluation.add_argument("--ingest", type=Path, help="run --app-dir: documents to give it first")
    evaluation.add_argument("--baseline", type=Path, help="run: a summary it mustn't fall below")
    evaluation.add_argument("--out", type=Path, help="run: write the summary here")
    evaluation.add_argument("--label", help="run: what this experiment tries")
    evaluation.add_argument("--case", help="grade: the case")
    evaluation.add_argument("--grader", help="grade: the human grader's name")
    evaluation.add_argument("--score", type=float, help="grade: from 0 to 1")
    evaluation.add_argument("--comment", help="grade: recorded with the grade")

    platform = commands.add_parser("platform", help="operator actions, with direct database access")
    platform.add_argument("--database-url", default=os.environ.get("AIOS_DATABASE_URL"))
    actions = platform.add_subparsers(dest="action", required=True)
    key = actions.add_parser("key", help="create an API key; printed once")
    key.add_argument("--tenant", required=True)
    key.add_argument("--name", required=True)
    key.add_argument("--role", action="append", default=[], help="default: tenant-admin")
    key.add_argument("--expires-in-days", type=int, default=None)
    stop = actions.add_parser("suspend", help="suspend a whole tenant")
    stop.add_argument("--tenant", required=True)
    stop.add_argument("--reason", required=True)
    lift = actions.add_parser("lift", help="end a tenant's suspension")
    lift.add_argument("--tenant", required=True)

    secrets = commands.add_parser("secrets", help="the encrypted local secret store")
    actions = secrets.add_subparsers(dest="action", required=True)
    actions.add_parser("new-key", help="print a new key for AIOS_SECRETS_KEY")
    put = actions.add_parser("set", help="store a secret; the value is read without echo")
    put.add_argument("name")
    put.add_argument("--tenant", required=True)
    put.add_argument("--app", required=True)

    docs = commands.add_parser("docs", help="the docs site's reference pages, made from the code")
    docs.add_argument("action", choices=["reference"])
    docs.add_argument("--dir", type=Path, default=Path("docs/reference"))
    docs.add_argument("--check", action="store_true", help="fail if a page is missing or stale")

    validate = commands.add_parser(
        "validate", help="check an App folder before AIOS runs it: no server needed"
    )
    validate.add_argument("app", type=Path, help="the App's folder")

    init = commands.add_parser(
        "init",
        help="a new App from a template; or an agent or a tool in one",
        description="aios init <app> [--template T] | aios init agent <name> --app <dir> | "
        "aios init tool <tool.name> --app <dir>",
    )
    init.add_argument("what", help="the new App's name, or 'agent' or 'tool'")
    init.add_argument("name", nargs="?", help="agent, tool: its name")
    init.add_argument("--dir", type=Path, default=Path(), help="an App: where to make it")
    init.add_argument(
        "--template",
        help="an App: chat (default), rag, approval, people, langgraph, openai-agents; "
        "an agent: python (default), langgraph, openai-agents",
    )
    init.add_argument("--app", type=Path, default=Path(), help="agent, tool: the App's folder")
    init.add_argument("--capability", help="agent: what it provides (its name, without -agent)")
    return parser


def _specs(args: argparse.Namespace) -> int:
    if args.action == "compat":
        from aios.core.compat import compare_folders  # noqa: PLC0415
        from aios.core.schemas import render_specs  # noqa: PLC0415

        if not (args.against / "schemas").is_dir():
            raise SystemExit(f"{args.against} has no schemas/ folder: is it a specs/ folder?")
        breaking = compare_folders(args.against, render_specs(generated_specs()))
        for change in breaking:
            print(f"breaking: {change}", file=sys.stderr)
        if breaking:
            print("give the schema a new version, or keep what readers rely on", file=sys.stderr)
            return 1
        print(f"compatible with {args.against.as_posix()}")
        return 0
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
    from aios.cli.server import Tls, check_exposure, serve  # noqa: PLC0415 - keeps --version light

    if args.command in ("api", "worker") and not args.database_url:
        raise SystemExit(f"aios {args.command} needs --database-url (or AIOS_DATABASE_URL)")
    if (args.tls_cert is None) != (args.tls_key is None):
        raise SystemExit("--tls-cert and --tls-key go together")
    check_exposure(args.command, args.host, no_auth=args.no_auth)
    tenants = args.tenant or [os.environ.get("AIOS_TENANT", DEFAULT_TENANT)]
    try:
        asyncio.run(
            serve(
                args.command,
                apps=args.app,
                host=args.host,
                port=args.port,
                tenants=tenants,
                database_url=args.database_url,
                cassettes=args.cassettes,
                cassette_mode=args.cassette_mode,
                log_level=args.log_level,
                no_auth=args.no_auth,
                tls=Tls(args.tls_cert, args.tls_key) if args.tls_cert else None,
            )
        )
    except KeyboardInterrupt:
        return 130
    return 0


def _platform(args: argparse.Namespace) -> int:
    from aios.cli import platform  # noqa: PLC0415 - needs the database driver

    url = args.database_url
    if not url:
        raise SystemExit("aios platform needs --database-url (or AIOS_DATABASE_URL)")
    if args.action == "key":
        roles = args.role or ["tenant-admin"]
        record, value = asyncio.run(
            platform.create_key(url, args.tenant, args.name, roles, days=args.expires_in_days)
        )
        print(json.dumps(record, indent=2), file=sys.stderr)
        print("this key is shown once:", file=sys.stderr)
        print(value)
        return 0
    if args.action == "suspend":
        asyncio.run(platform.suspend_tenant(url, args.tenant, args.reason))
        print(f"suspended: {args.tenant}")
        return 0
    if not asyncio.run(platform.lift_tenant(url, args.tenant)):
        raise SystemExit(f"{args.tenant} is not suspended")
    print(f"lifted: {args.tenant}")
    return 0


def _secrets(args: argparse.Namespace) -> int:
    from aios.security.secrets import EncryptedFileSecrets, new_store_key  # noqa: PLC0415

    if args.action == "new-key":
        print(new_store_key())
        return 0
    path, key = os.environ.get("AIOS_SECRETS_FILE"), os.environ.get("AIOS_SECRETS_KEY")
    if not path or not key:
        raise SystemExit("aios secrets set needs AIOS_SECRETS_FILE and AIOS_SECRETS_KEY")
    value = getpass.getpass(f"{args.name}: ") if sys.stdin.isatty() else sys.stdin.read().strip()
    if not value:
        raise SystemExit("no value given")
    EncryptedFileSecrets(path, key).put(args.tenant, args.app, args.name, value)
    print(f"stored {args.name} for {args.tenant}/{args.app}")
    return 0


def _signal_data(text: str | None) -> object:
    """``--data`` as JSON if it parses, otherwise as plain text."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _security_command(  # noqa: PLR0911 - one branch per command
    api: Client, args: argparse.Namespace
) -> int:
    if args.command == "audit":
        if args.action == "verify":
            report = api.audit_verify()
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        print(json.dumps(api.audit(args.after, args.limit), indent=2))
        return 0
    if args.command == "suspend":
        print(json.dumps(api.suspend(args.kind, args.name, args.reason), indent=2))
        return 0
    if args.command == "unsuspend":
        api.unsuspend(args.kind, args.name)
        print(f"lifted: {args.kind} {args.name}")
        return 0
    if args.command == "suspensions":
        print(json.dumps(api.suspensions(), indent=2))
        return 0
    if args.action == "list":
        print(json.dumps(api.keys(), indent=2))
        return 0
    if not args.name:
        raise SystemExit(f"aios keys {args.action} needs a name or an id")
    if args.action == "revoke":
        api.revoke_key(args.name)
        print(f"revoked: {args.name}")
        return 0
    created = api.create_key(args.name, args.role or ["member"], args.expires_in_days)
    value = created.pop("key")
    print(json.dumps(created, indent=2), file=sys.stderr)
    print("this key is shown once:", file=sys.stderr)
    print(value)
    return 0


def _memory_command(api: Client, args: argparse.Namespace) -> int:
    from aios.memory import documents_in  # noqa: PLC0415 - keeps `aios --version` light

    if args.action == "forget":
        if (args.item is None) == (args.subject is None):
            raise SystemExit("aios memory forget needs --item or --subject")
        print(json.dumps(api.memory_forget(item=args.item, subject=args.subject), indent=2))
        return 0
    if not args.target:
        raise SystemExit(f"aios memory {args.action} needs a target")
    if args.action == "search":
        scopes = args.scope or ["app", "tenant", "user"]
        print(json.dumps(api.memory_search(args.target, scopes, args.k), indent=2))
        return 0
    if args.action == "get":
        print(json.dumps(api.memory_item(args.target), indent=2))
        return 0
    if not args.app:
        raise SystemExit("aios memory ingest needs --app")
    scope = (args.scope or ["app"])[0]
    if args.target.startswith(("http://", "https://")):
        body = {"url": args.target, "scope": scope, "sensitivity": args.sensitivity}
        print(json.dumps(api.ingest_url(args.app, body), indent=2))
        return 0
    source, documents = documents_in(Path(args.target))
    report = api.ingest(
        args.app,
        {
            "source": source,
            "documents": [{"uri": d.uri, "text": d.text} for d in documents],
            "scope": scope,
            "sensitivity": args.sensitivity,
            "prune": not args.keep,
        },
    )
    print(json.dumps(report, indent=2))
    return 0


def _approvals_command(api: Client, args: argparse.Namespace) -> int:
    if args.action == "list":
        print(json.dumps(api.approvals(args.status, args.all), indent=2))
        return 0
    if not args.id:
        raise SystemExit(f"aios approvals {args.action} needs an approval id")
    if args.action == "show":
        print(json.dumps(api.approval(args.id), indent=2))
        return 0
    edits = dict(_pair(text, "--edit") for text in args.edit)
    print(json.dumps(api.answer(args.id, args.action, args.comment, edits), indent=2))
    return 0


def _init(args: argparse.Namespace) -> int:
    from aios.cli import scaffold  # noqa: PLC0415

    if args.what in ("agent", "tool"):
        if not args.name:
            raise SystemExit(f"aios init {args.what} needs a name")
        done = (
            scaffold.add_agent(args.name, args.app, args.template or "python", args.capability)
            if args.what == "agent"
            else scaffold.add_tool(args.name, args.app)
        )
        print("\n".join(done))
        return 0
    root = scaffold.init(args.what, args.dir, args.template or "chat")
    print(f"created {root.as_posix()} from the {args.template or 'chat'} template")
    print(f"next: read {(root / 'README.md').as_posix()}, then run its tests with pytest")
    return 0


def _feedback_command(api: Any, args: argparse.Namespace) -> int:
    import yaml  # noqa: PLC0415 - only this command prints YAML

    if args.action == "list":
        print(json.dumps(api.feedback(args.app, args.run_id), indent=2))
        return 0
    if args.action == "cases":
        print(yaml.safe_dump({"cases": api.feedback_cases(args.app)}, sort_keys=False), end="")
        return 0
    if not args.run_id:
        raise SystemExit("aios feedback give needs the run's id")
    body: dict[str, Any] = {
        "rating": args.rating,
        "step": args.step,
        "comment": args.comment,
        "remember": args.remember,
    }
    if args.correction is not None and args.correct:
        raise SystemExit("give --correction or --correct, not both")
    if args.correct:
        body["correction"] = dict(_pair(text, "--correct") for text in args.correct)
    elif args.correction is not None:
        try:
            body["correction"] = json.loads(args.correction)
        except ValueError:
            body["correction"] = args.correction
    print(json.dumps(api.give_feedback(args.app, args.run_id, body), indent=2))
    return 0


def _tasks_command(api: Any, args: argparse.Namespace) -> int:
    if args.action == "list":
        print(json.dumps(api.tasks(args.all), indent=2))
        return 0
    if not args.id:
        raise SystemExit(f"aios tasks {args.action} needs a task id")
    if args.action == "show":
        print(json.dumps(api.task(args.id), indent=2))
        return 0
    if args.output is not None and args.field:
        raise SystemExit("give --output or --field, not both")
    if args.field:
        output: Any = dict(_pair(text, "--field") for text in args.field)
    elif args.output is not None:
        try:
            output = json.loads(args.output)
        except ValueError:
            output = args.output
    else:
        raise SystemExit("aios tasks complete needs --output or --field: the result of the work")
    print(json.dumps(api.complete_task(args.id, output, args.comment), indent=2))
    return 0


def _pair(text: str, option: str) -> tuple[str, Any]:
    """``amount=80`` as ``("amount", 80)``. JSON is read as JSON; anything else is text. Every
    shell passes these intact, where Windows PowerShell 5.1 strips the quotes inside JSON."""
    name, found, value = text.partition("=")
    if not found or not name:
        raise SystemExit(f"{option} takes NAME=VALUE, not {text!r}")
    try:
        return name, json.loads(value)
    except ValueError:
        return name, value


def _client(args: argparse.Namespace) -> int:
    import httpx2  # noqa: PLC0415 - only the client commands need it

    from aios.cli import client  # noqa: PLC0415

    api = client.Client(args.server, args.tenant, token=os.environ.get("AIOS_API_KEY"))
    try:
        return _command(api, args)
    except httpx2.TransportError as exc:  # nothing answered: say where, not a traceback
        raise SystemExit(
            f"no answer from AIOS at {api.server} ({exc}). "
            "Is it running? Check --server or AIOS_SERVER"
        ) from None


def _command(api: Client, args: argparse.Namespace) -> int:  # noqa: PLR0911, PLR0912 - one per command
    from aios.cli import client  # noqa: PLC0415

    if args.command in ("audit", "keys", "suspend", "unsuspend", "suspensions"):
        return _security_command(api, args)
    if args.command == "memory":
        return _memory_command(api, args)
    if args.command == "approvals":
        return _approvals_command(api, args)
    if args.command == "run":
        return client.run_and_follow(api, args.app, args.message)
    if args.command == "replay":
        return client.restart_and_follow(api, args.app, args.run_id, "replay")
    if args.command in ("fork", "retry"):
        changes = _changes(args)
        return client.restart_and_follow(api, args.app, args.run_id, args.command, body=changes)
    if args.command == "compare":
        return client.compare(api, args.app, args.run_id, args.other)
    if args.command == "trace":
        return client.trace(api, args.app, args.run_id)
    if args.command == "logs":
        return client.logs(api, args.app, args.run_id)
    if args.command == "status":
        print(json.dumps(api.run(args.app, args.run_id), indent=2))
        return 0
    if args.command in ("pause", "resume", "cancel"):
        print(json.dumps(api.control(args.app, args.run_id, args.command), indent=2))
        return 0
    if args.command == "signal":
        reply = api.signal(args.app, args.run_id, args.name, _signal_data(args.data))
        print(json.dumps(reply, indent=2))
        return 0
    if args.command == "apply":
        print(json.dumps(api.apply(args.file, consent=args.yes), indent=2))
        return 0
    if args.command == "app":
        from aios.cli import apps  # noqa: PLC0415

        return apps.run(api, args)
    if args.command == "tasks":
        return _tasks_command(api, args)
    if args.command == "describe":
        from aios.cli.describe import describe  # noqa: PLC0415

        print(describe(api, args.kind, args.name, app=args.app))
        return 0
    if args.command == "feedback":
        return _feedback_command(api, args)
    if args.command == "groups":
        if args.action == "list":
            print(json.dumps(api.groups(), indent=2))
            return 0
        if not args.name:
            raise SystemExit("aios groups set needs the group's name")
        print(json.dumps(api.set_group(args.name, args.member), indent=2))
        return 0
    if args.command == "eval":
        from aios.cli import evals  # noqa: PLC0415

        return evals.run(api, args)
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


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911 - one per command
    args = _parser().parse_args(argv)
    if args.command == "specs":
        return _specs(args)
    if args.command in ("dev", "api", "worker"):
        return _serve(args)
    if args.command == "platform":
        return _platform(args)
    if args.command == "secrets":
        return _secrets(args)
    if args.command == "init":
        return _init(args)
    if args.command == "docs":
        from aios.cli import docs as reference  # noqa: PLC0415

        stale = reference.write(args.dir, _parser(), check=args.check)
        for path in stale:
            print(f"{'stale' if args.check else 'wrote'}: {path.as_posix()}")
        return 1 if args.check and stale else 0
    if args.command == "validate":
        from aios.cli.validate import validate  # noqa: PLC0415

        report = validate(args.app)
        print(report.text())
        return 0 if report.ok else 1
    return _client(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
