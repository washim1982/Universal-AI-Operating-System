"""``aios describe``: one thing, in words a person reads (FR-CP-03; WP 3.6).

    aios describe app ai-researcher
    aios describe run <run-id> --app ai-researcher
    aios describe agent research-agent
    aios describe approval <approval-id>
    aios describe task <task-id>

``aios get`` and ``aios status`` give JSON for scripts; this is for people.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

KINDS = ("app", "run", "agent", "approval", "task")
WIDTH = 16


class Api(Protocol):
    def app(self, app: str) -> Any: ...
    def runs(self, app: str, limit: int) -> Any: ...
    def run(self, app: str, run_id: str) -> Any: ...
    def trace_of(self, app: str, run_id: str) -> Any: ...
    def agents(self) -> Any: ...
    def approval(self, approval: str) -> Any: ...
    def task(self, task: str) -> Any: ...


def describe(api: Api, kind: str, name: str, *, app: str | None = None) -> str:
    if kind == "app":
        return app_text(api.app(name), api.runs(name, 5))
    if kind == "run":
        if not app:
            raise SystemExit("aios describe run needs --app")
        return run_text(api.run(app, name), api.trace_of(app, name))
    if kind == "agent":
        found = [a for a in api.agents() if a["agent"] == name and (app is None or a["app"] == app)]
        if not found:
            raise SystemExit(f"no agent {name}" + (f" in {app}" if app else ""))
        return "\n\n".join(agent_text(agent) for agent in found)
    if kind == "approval":
        return approval_text(api.approval(name))
    return task_text(api.task(name))


def _rows(pairs: Iterable[tuple[str, Any]]) -> list[str]:
    return [f"{key + ':':<{WIDTH}}{value}" for key, value in pairs if value not in (None, "", [])]


def _list(items: Sequence[str], empty: str = "none") -> str:
    return ", ".join(items) if items else empty


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _indented(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


def app_text(app: Mapping[str, Any], runs: Sequence[Mapping[str, Any]]) -> str:
    lines = _rows(
        [
            ("App", f"{app['app']} {app['version']}"),
            ("Description", app.get("description")),
            (
                "Entry points",
                _list([f"{e['type']} -> {e['workflow']}" for e in app.get("entrypoints", [])]),
            ),
            ("Policies", _list(app.get("policies", []))),
            ("Asks for", _list(app.get("permissions", {}).get("tools", []), "no tools")),
        ]
    )
    lines.append("Agents:")
    for agent in app.get("agents", []):
        tools = _list(agent.get("tools", []), "no tools")
        lines.append(
            f"  {agent['agent']} ({agent['runtime']}): provides {_list(agent['capabilities'])};"
            f" may use {tools}"
        )
    if app.get("humans"):
        lines.append("Done by people:")
        lines += [f"  {h['capability']}: {_list(h['assignees'])}" for h in app.get("humans", [])]
    lines.append("Workflows:")
    for workflow in app.get("workflows", []):
        lines.append(f"  {workflow['workflow']} {workflow['version']}")
        for step in workflow.get("steps", []):
            what = step.get("capability") or step.get("tool") or step["type"]
            needs = f" after {_list(step['needs'])}" if step.get("needs") else ""
            lines.append(f"    {step['id']}: {step['type']} {what}{needs}")
    lines.append("Recent runs:" if runs else "Recent runs:    none yet")
    lines += [
        f"  {run['run_id']}  {run['status']:<10} {run.get('started_at') or ''}" for run in runs
    ]
    return "\n".join(lines)


def run_text(run: Mapping[str, Any], trace: Mapping[str, Any]) -> str:
    usage: Mapping[str, Any] = run.get("usage") or {}
    lines = _rows(
        [
            ("Run", run["run_id"]),
            ("App", f"{run['app']} ({trace.get('workflow')} {trace.get('version')})"),
            ("Status", run["status"]),
            ("Waiting for", _list(run.get("waiting", []), "")),
            ("Started", trace.get("started_at")),
            ("Took", _took(trace.get("duration_ms"))),
            (
                "Cost",
                f"${usage.get('cost_usd', trace.get('cost_usd', 0)):.4f}, "
                f"{trace.get('input_tokens', 0)} tokens in, {trace.get('output_tokens', 0)} out",
            ),
            ("Tainted", "yes: it read untrusted content" if run.get("tainted") else None),
            ("From", _source(run.get("source"))),
        ]
    )
    lines.append("Steps:")
    for node in trace.get("nodes", []):
        what = node.get("capability") or node.get("tool") or node["type"]
        calls = f"{len(node.get('model_calls', []))} model, {len(node.get('tool_calls', []))} tool"
        took = _took(node.get("duration_ms"))
        detail = f"  ({calls} calls{', ' + took if took else ''})"
        lines.append(f"  {node['id']:<14} {node['status']:<10} {what}{detail}")
        if node.get("error"):
            lines.append(f"    error: {node['error']}")
        for approval in node.get("approvals", []):
            by = f" by {approval['by']}" if approval.get("by") else ""
            lines.append(f"    approval {approval['approval']}: {approval['status']}{by}")
    if run.get("error"):
        lines.append(f"Error:          {run['error'].get('message')}")
    if run.get("output") is not None:
        lines += ["Output:", _indented(_json(run["output"]))]
    return "\n".join(lines)


def _took(ms: float | None) -> str | None:
    if ms is None:
        return None
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _source(source: Mapping[str, Any] | None) -> str | None:
    if not source:
        return None
    step = f" from step {source['step']}" if source.get("step") else ""
    return f"{source.get('mode', 'a copy')} of {source.get('run')}{step}"


def agent_text(agent: Mapping[str, Any]) -> str:
    stopped = f"stopped: {agent.get('suspension')}" if agent.get("suspended") else None
    return "\n".join(
        _rows(
            [
                ("Agent", f"{agent['agent']} {agent['version']}"),
                ("App", agent["app"]),
                ("Provides", _list(agent.get("capabilities", []))),
                ("May use", _list(agent.get("tools", []), "no tools")),
                ("Refused tools", _list(agent.get("refused_tools", []), "")),
                ("Memory", _memory(agent)),
                ("State", stopped or "working"),
                (
                    "Recent tasks",
                    f"{agent.get('completed', 0)} completed, {agent.get('failed', 0)} failed",
                ),
                ("Last active", agent.get("last_active")),
            ]
        )
    )


def _memory(agent: Mapping[str, Any]) -> str:
    read, write = agent.get("memory_read", []), agent.get("memory_write", [])
    return f"reads {_list(read)}; writes {_list(write)}"


def approval_text(approval: Mapping[str, Any]) -> str:
    votes: list[Any] = approval.get("votes") or []
    quorum = approval.get("quorum", 1)
    lines = _rows(
        [
            ("Approval", approval["approval"]),
            ("Status", approval["status"]),
            ("Asks", approval["summary"]),
            ("Risk", approval["risk"]),
            ("App", approval["app"]),
            ("Run", approval.get("run")),
            ("For", approval.get("initiator")),
            ("Who can answer", _list(approval.get("approvers", []))),
            ("Approvals", f"{len(votes)} of {quorum}" if quorum > 1 else None),
            ("Rules", _list(approval.get("policies", []))),
            ("Expires", approval.get("expires_at")),
            ("Answered by", approval.get("by")),
            ("Comment", approval.get("comment")),
            (
                "Tainted",
                "yes: the agent read untrusted content first" if approval.get("tainted") else None,
            ),
        ]
    )
    if approval.get("preview") is not None:
        preview = approval["preview"]
        lines += ["It would:", _indented(preview if isinstance(preview, str) else _json(preview))]
    lines += ["Exactly:", _indented(_json(approval.get("payload")))]
    if approval.get("edits"):
        lines += ["Changed to:", _indented(_json(approval["edits"]))]
    if approval.get("rationale"):
        lines += ["The agent says (a claim, not a fact):", _indented(approval["rationale"])]
    return "\n".join(lines)


def task_text(task: Mapping[str, Any]) -> str:
    lines = _rows(
        [
            ("Task", task["task"]),
            ("Status", task["status"]),
            ("Capability", task["capability"]),
            ("What to do", task.get("description")),
            ("App", task["app"]),
            ("Run", task.get("run")),
            ("Assigned to", _list(task.get("assignees", []))),
            ("Done by", task.get("by")),
        ]
    )
    lines += ["Given:", _indented(_json(task.get("input")))]
    if task.get("output_schema"):
        lines += ["The answer must fit:", _indented(_json(task["output_schema"]))]
    if task.get("status") == "completed":
        lines += ["Answer:", _indented(_json(task.get("output")))]
    return "\n".join(lines)
