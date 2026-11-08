"""``aios eval``: run an evaluation suite, compare experiments, grade by hand (FR-OBS-07).

``aios eval run`` with ``--app-dir`` runs the suite here, in this process, with the App in that
folder: what CI does, with recorded model answers. Without it, the server runs it, on the App as
installed. Either way the gate decides the exit code: 0 when the experiment may ship, 1 when not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from aios.core.evals import Experiment, check_gate
from aios.core.identity import Identity
from aios.core.manifests import EvaluationManifest, load_evaluation

if TYPE_CHECKING:
    from aios.cli.client import Client

LOCAL_TENANT = "local"
WAIT_SECONDS = 1.0


def run(api: Client, args: argparse.Namespace) -> int:
    """The ``aios eval`` actions."""
    if args.action == "run":
        return _run(api, args)
    if args.action == "online":
        print(json.dumps(api.online(_app(args)), indent=2))
        return 0
    if args.action == "list":
        for found in api.experiments(_app(args)):
            report_line(found)
        return 0
    if not args.target:
        raise SystemExit(f"aios eval {args.action} needs an experiment id")
    if args.action == "show":
        report(api.experiment(_app(args), args.target))
        return 0
    if args.action == "compare":
        if not args.other:
            raise SystemExit("aios eval compare needs two experiment ids")
        return _compare(api.experiment_compare(_app(args), args.target, args.other))
    if args.case is None or args.grader is None or args.score is None:
        raise SystemExit("aios eval grade needs --case, --grader and --score")
    found = api.experiment_grade(
        _app(args),
        args.target,
        {"case": args.case, "grader": args.grader, "score": args.score, "comment": args.comment},
    )
    report(found)
    return 0


def _app(args: argparse.Namespace, suite: EvaluationManifest | None = None) -> str:
    app = args.app or (suite.spec.app if suite is not None else None)
    if not app:
        raise SystemExit("say which App: --app, or spec.app in the suite")
    return app


def _run(api: Client, args: argparse.Namespace) -> int:
    if not args.target:
        raise SystemExit("aios eval run needs the suite's YAML file")
    suite = load_evaluation(Path(args.target))
    baseline = (
        Experiment.from_summary(json.loads(args.baseline.read_text("utf-8-sig")))
        if args.baseline
        else None
    )
    if args.app_dir is not None:
        summary = asyncio.run(_local(args, suite))
    else:
        app = _app(args, suite)
        body = {"suite": suite.model_dump(mode="json", by_alias=True), "label": args.label}
        experiment = api.experiment_run(app, body)["experiment"]
        print(f"experiment {experiment}", flush=True)
        summary = _waited(lambda: api.experiment(app, experiment), _deadline(suite))
    if args.out is not None:
        args.out.write_text(json.dumps(summary, indent=2) + "\n", "utf-8")
    found = Experiment.from_summary(summary)
    gate = check_gate(found, suite.spec.gate, baseline)
    report({**summary, "gate_result": gate.model_dump(mode="json")})
    return 0 if gate.passed else 1


def _deadline(suite: EvaluationManifest) -> float:
    per_case = suite.spec.timeout.total_seconds() if suite.spec.timeout else 600.0
    return time.monotonic() + per_case * len(suite.spec.cases) + 60


def _waited(read: Any, deadline: float) -> dict[str, Any]:
    while True:
        found: dict[str, Any] = read()
        if found.get("status") == "completed":
            return found
        if time.monotonic() > deadline:
            raise SystemExit(f"experiment {found.get('experiment')} didn't finish in time")
        time.sleep(WAIT_SECONDS)


async def _local(args: argparse.Namespace, suite: EvaluationManifest) -> dict[str, Any]:
    """Run the suite in this process, with the App in ``--app-dir``: no server, no database."""
    from aios.cli.server import running  # noqa: PLC0415 - starts the whole runtime
    from aios.controlplane import ANONYMOUS  # noqa: PLC0415
    from aios.memory import documents_in  # noqa: PLC0415

    async with running(
        "dev",
        apps=[args.app_dir],
        tenants=[LOCAL_TENANT],
        database_url=None,
        cassettes=args.cassettes,
        cassette_mode=args.cassette_mode,
    ) as (runtime, _):
        app = args.app or suite.spec.app or next(iter(runtime.apps.values())).name
        if args.ingest is not None:
            source, documents = documents_in(args.ingest)
            by = Identity("system:platform", LOCAL_TENANT)
            await runtime.memory.sync(by, app, documents, source=source)
        evaluator = runtime.evaluator
        experiment = await evaluator.request(
            LOCAL_TENANT, app, suite, principal=ANONYMOUS, on_behalf_of=None, label=args.label
        )
        print(f"experiment {experiment}", flush=True)
        deadline = _deadline(suite)
        while True:
            found = await evaluator.experiment(LOCAL_TENANT, app, experiment)
            if found is not None and found.status == "completed":
                return found.summary()
            if time.monotonic() > deadline:
                raise SystemExit(f"experiment {experiment} didn't finish in time")
            await asyncio.sleep(0.2)


def _score(value: Any) -> str:
    return "-" if value is None else f"{float(value):.3f}"


def report_line(found: dict[str, Any], out: TextIO | None = None) -> None:
    out = out or sys.stdout
    label = f" ({found['label']})" if found.get("label") else ""
    print(
        f"{found['experiment']}  {found['status']:<9}  {found['suite']} {found['suite_version']}"
        f"  on {found['app']} {found.get('app_version') or ''}  score {_score(found['score'])}"
        f"{label}",
        file=out,
    )


def report(found: dict[str, Any], out: TextIO | None = None) -> None:
    """An experiment, case by case, with its figures and its gate."""
    out = out or sys.stdout
    report_line(found, out)
    for case in found.get("cases", []):
        grades = ", ".join(
            f"{g['grader']} {'waiting' if g['score'] is None else _score(g['score'])}"
            for g in case["grades"]
        )
        score = _score(case["score"])
        print(f"  {case['case']:<20} {case['status']:<10} {score:<6} {grades}", file=out)
        if case.get("error"):
            print(f"    {case['error']}", file=out)
    print(
        f"score {_score(found['score'])}, {found['passed']} passed, {found['failed']} failed"
        f", {found['pending']} waiting for a person, {found['drifted']} drifted"
        f", ${found['cost_usd']:.4f}",
        file=out,
    )
    if found.get("metrics"):
        metrics = ", ".join(f"{name} {value:.3f}" for name, value in found["metrics"].items())
        print(f"metrics: {metrics}", file=out)
    gate = found.get("gate_result")
    if gate is not None:
        verdict = "passed" if gate["passed"] else "FAILED"
        reasons = "; ".join(gate["reasons"])
        print(f"gate: {verdict}{f' - {reasons}' if reasons else ''}", file=out)


def _compare(found: dict[str, Any], out: TextIO | None = None) -> int:
    out = out or sys.stdout
    a, b = found["score"]
    print(f"{'':<20} {found['a']:<28} {found['b']}", file=out)
    print(f"{'score':<20} {_score(a):<28} {_score(b)}", file=out)
    for name, (x, y) in found["metrics"].items():
        print(f"{name:<20} {_score(x):<28} {_score(y)}", file=out)
    for case in found["cases"]:
        change = case["change"]
        delta = "" if change is None else f"  ({change:+.3f})"
        print(f"{case['case']:<20} {_score(case['a']):<28} {_score(case['b'])}{delta}", file=out)
    return 0
