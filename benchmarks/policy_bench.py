"""Policy decision latency for the Cedar engine (ADR-007 spike, NFR-08).

Compiles the AI Support Desk policy with its default configuration and times
``CedarDecisionPoint.decide_now`` on a mix of requests that exercise every outcome: allow,
deny, default deny, approval (two evaluations) and delegation (two principals).

    uv run python benchmarks/policy_bench.py --decisions 20000
    uv run python benchmarks/policy_bench.py --json results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from aios.core.manifests import PolicyManifest, load_manifests
from aios.core.policy import Actor, PolicyAction, PolicyRequest, Resource, SideEffect
from aios.security.cedar import CedarDecisionPoint

ROOT = Path(__file__).resolve().parents[1]
DESK = ROOT / "apps" / "ai-support-desk"


@dataclass(frozen=True)
class Result:
    decisions: int
    policies: int
    p50_us: float
    p99_us: float
    max_us: float
    per_second: float


def engine() -> CedarDecisionPoint:
    schema = json.loads((DESK / "config.schema.json").read_text(encoding="utf-8"))
    config = {name: spec["default"] for name, spec in schema["properties"].items()}
    path = DESK / "policies" / "support-desk-policy.yaml"
    policies = [m for m in load_manifests(path, config=config) if isinstance(m, PolicyManifest)]
    return CedarDecisionPoint.from_manifests(policies, tenant="acme")


def requests() -> list[PolicyRequest]:
    def tool(name: str, effect: SideEffect, **fields: object) -> PolicyRequest:
        return PolicyRequest(
            tenant="acme",
            app="ai-support-desk",
            actor=Actor("agent:refund-agent@acme"),
            action=PolicyAction.TOOL_INVOKE,
            resource=Resource.tool(name, effect),
            **fields,  # type: ignore[arg-type]
        )

    user = Actor("user:alice@acme", frozenset({"support"}))
    return [
        tool("zendesk.ticket.get", SideEffect.READ),  # allow
        tool("stripe.refund.create", SideEffect.FINANCIAL, input={"amountUSD": 20}),  # allow
        tool("stripe.refund.create", SideEffect.FINANCIAL, input={"amountUSD": 120.5}),  # approval
        tool("stripe.customer.delete", SideEffect.DESTRUCTIVE),  # deny
        tool("github.pr.create", SideEffect.WRITE),  # default deny
        tool("zendesk.ticket.reply", SideEffect.EXTERNAL_COMM, tainted=True),  # approval
        tool("zendesk.ticket.get", SideEffect.READ, on_behalf_of=user),  # delegation
    ]


def run(decisions: int) -> Result:
    pdp = engine()
    mix = requests()
    for request in mix:  # warm up
        pdp.decide_now(request)
    timings: list[float] = []
    started = time.perf_counter()
    for index in range(decisions):
        request = mix[index % len(mix)]
        before = time.perf_counter()
        pdp.decide_now(request)
        timings.append(time.perf_counter() - before)
    elapsed = time.perf_counter() - started
    ordered = sorted(timings)
    return Result(
        decisions=decisions,
        policies=len(pdp.compiled.rules),
        p50_us=round(statistics.median(ordered) * 1e6, 1),
        p99_us=round(ordered[min(len(ordered) - 1, round(0.99 * (len(ordered) - 1)))] * 1e6, 1),
        max_us=round(ordered[-1] * 1e6, 1),
        per_second=round(decisions / elapsed, 1),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Cedar policy decision latency")
    parser.add_argument("--decisions", type=int, default=20_000)
    parser.add_argument("--json", type=Path, default=None, help="also write the result here")
    args = parser.parse_args()
    result = run(args.decisions)
    print(json.dumps(asdict(result), indent=2))
    if args.json:
        args.json.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
