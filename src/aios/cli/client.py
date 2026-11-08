"""CLI commands that talk to a running AIOS API (WP 1.7; FR-CP-03)."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO, cast

import httpx2

from aios.core.apps import Bundle
from aios.core.envelope import Event
from aios.observability import render

TERMINAL = ("run.completed", "run.failed", "run.cancelled")


class Client:
    def __init__(
        self,
        server: str,
        tenant: str | None = None,
        timeout: float = 30.0,
        *,
        token: str | None = None,
    ) -> None:
        headers = {"X-AIOS-Tenant": tenant} if tenant else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.server = server.rstrip("/")
        self.http = httpx2.Client(base_url=self.server, headers=headers, timeout=timeout)

    def _json(self, response: httpx2.Response) -> Any:
        if response.status_code >= 400:
            detail = (
                response.json().get("detail")
                if response.headers.get("content-type", "").startswith("application/json")
                else response.text
            )
            if detail is None:  # every AIOS error has one; llama-server also defaults to 8080
                detail = f"{self.server} did not answer like AIOS; check --server or AIOS_SERVER"
            raise SystemExit(f"error {response.status_code}: {detail}")
        return response.json() if response.content else None

    def apply(self, path: Path, *, consent: bool = False) -> list[Any]:
        files = sorted(path.rglob("*.yaml")) if path.is_dir() else [path]
        text = "\n---\n".join(f.read_text(encoding="utf-8") for f in files)
        response = self.http.post(
            "/v1/apply",
            params={"consent": "all" if consent else "ask"},
            content=text,
            headers={"Content-Type": "application/yaml"},
        )
        return [self._json(response)]

    def restart(self, app: str, run_id: str, mode: str, body: dict[str, Any]) -> str:
        """Replay, fork or retry a run; returns the new run's id."""
        path = f"/v1/apps/{app}/runs/{run_id}/{mode}"
        return str(self._json(self.http.post(path, json=body))["run_id"])

    def compare(self, app: str, a: str, b: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/runs/{a}/compare/{b}"))

    def groups(self) -> Any:
        return self._json(self.http.get("/v1/groups"))

    def set_group(self, group: str, members: list[str]) -> Any:
        return self._json(self.http.put(f"/v1/groups/{group}", json={"members": members}))

    def experiment_run(self, app: str, body: dict[str, Any]) -> Any:
        return self._json(self.http.post(f"/v1/apps/{app}/experiments", json=body))

    def experiments(self, app: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/experiments"))

    def experiment(self, app: str, experiment: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/experiments/{experiment}"))

    def experiment_compare(self, app: str, a: str, b: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/experiments/{a}/compare/{b}"))

    def experiment_grade(self, app: str, experiment: str, body: dict[str, Any]) -> Any:
        path = f"/v1/apps/{app}/experiments/{experiment}/grades"
        return self._json(self.http.post(path, json=body))

    def online(self, app: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/evaluations/online"))

    def install(self, bundle: Bundle, config: dict[str, Any] | None = None) -> Any:
        body = {"bundle": bundle.model_dump(mode="json"), "config": config}
        return self._json(self.http.post("/v1/installations", json=body))

    def installations(self) -> Any:
        return self._json(self.http.get("/v1/installations"))

    def installation(self, app: str) -> Any:
        return self._json(self.http.get(f"/v1/installations/{app}"))

    def consent(self, app: str, grants: dict[str, Any] | None = None) -> Any:
        body = {"grants": grants} if grants is not None else {}
        return self._json(self.http.post(f"/v1/installations/{app}/consent", json=body))

    def refuse(self, app: str) -> Any:
        return self._json(self.http.post(f"/v1/installations/{app}/refuse"))

    def rollback(self, app: str, version: str) -> Any:
        body = {"version": version}
        return self._json(self.http.post(f"/v1/installations/{app}/rollback", json=body))

    def configure(self, app: str, config: dict[str, Any]) -> Any:
        return self._json(self.http.put(f"/v1/installations/{app}/config", json=config))

    def export(self, app: str, target: Path) -> int:
        """Save everything an App has, and say how many lines that was."""
        lines = 0
        with self.http.stream("GET", f"/v1/installations/{app}/export") as response:
            if response.status_code >= 400:
                raise SystemExit(f"error {response.status_code}: can't export {app}")
            with target.open("w", encoding="utf-8") as file:
                for line in response.iter_lines():
                    file.write(line + "\n")
                    lines += 1
        return lines

    def uninstall(self, app: str, data: str) -> Any:
        return self._json(self.http.delete(f"/v1/installations/{app}", params={"data": data}))

    def apps(self) -> Any:
        return self._json(self.http.get("/v1/apps"))

    def start_run(self, app: str, message: str) -> str:
        return str(
            self._json(self.http.post(f"/v1/apps/{app}/runs", json={"message": message}))["run_id"]
        )

    def app(self, app: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}"))

    def runs(self, app: str, limit: int = 50) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/runs", params={"limit": limit}))

    def trace_of(self, app: str, run_id: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/runs/{run_id}/trace"))

    def agents(self) -> Any:
        return self._json(self.http.get("/v1/agents"))

    def run(self, app: str, run_id: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/runs/{run_id}"))

    def control(self, app: str, run_id: str, action: str) -> Any:
        """``cancel``, ``pause`` or ``resume`` a run."""
        return self._json(self.http.post(f"/v1/apps/{app}/runs/{run_id}/{action}"))

    def signal(self, app: str, run_id: str, name: str, data: Any) -> Any:
        body = {"name": name, "data": data}
        return self._json(self.http.post(f"/v1/apps/{app}/runs/{run_id}/signal", json=body))

    def events(self, app: str, run_id: str) -> list[Event]:
        data = self._json(self.http.get(f"/v1/apps/{app}/runs/{run_id}/events"))
        return [Event.model_validate(item) for item in data]

    def stream(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        with self.http.stream(
            "GET", path, params=params, headers={"Accept": "text/event-stream"}, timeout=None
        ) as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def log(
        self, app: str | None, pattern: str, after: int, limit: int, *, newest: bool = False
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"pattern": pattern, "after": after, "limit": limit}
        if newest:
            params["newest"] = "true"
        if app is not None:
            params["app"] = app
        return self._json(self.http.get("/v1/events", params=params))

    def dlq(self) -> Any:
        return self._json(self.http.get("/v1/dlq"))

    def redrive(self, letter: str) -> Any:
        return self._json(self.http.post(f"/v1/dlq/{letter}/redrive"))

    def discard(self, letter: str) -> None:
        self._json(self.http.delete(f"/v1/dlq/{letter}"))

    def audit(self, after: int, limit: int) -> Any:
        return self._json(self.http.get("/v1/audit", params={"after": after, "limit": limit}))

    def audit_verify(self) -> Any:
        return self._json(self.http.get("/v1/audit/verify"))

    def keys(self) -> Any:
        return self._json(self.http.get("/v1/keys"))

    def create_key(self, name: str, roles: list[str], days: int | None) -> Any:
        body = {"name": name, "roles": roles, "expires_in_days": days}
        return self._json(self.http.post("/v1/keys", json=body))

    def revoke_key(self, key: str) -> None:
        self._json(self.http.delete(f"/v1/keys/{key}"))

    def suspend(self, kind: str, name: str, reason: str) -> Any:
        body = {"kind": kind, "name": name, "reason": reason}
        return self._json(self.http.post("/v1/suspensions", json=body))

    def unsuspend(self, kind: str, name: str) -> None:
        self._json(self.http.delete(f"/v1/suspensions/{kind}/{name}"))

    def suspensions(self) -> Any:
        return self._json(self.http.get("/v1/suspensions"))

    def memory_search(self, query: str, scopes: list[str], k: int) -> Any:
        body = {"query": query, "scopes": scopes, "k": k}
        return self._json(self.http.post("/v1/memory/search", json=body))

    def memory_item(self, item: str) -> Any:
        return self._json(self.http.get(f"/v1/memory/{item}"))

    def memory_forget(self, *, item: str | None, subject: str | None) -> Any:
        body = {"item": item, "subject": subject}
        return self._json(self.http.post("/v1/memory/forget", json=body))

    def give_feedback(self, app: str, run: str, body: dict[str, Any]) -> Any:
        return self._json(self.http.post(f"/v1/apps/{app}/runs/{run}/feedback", json=body))

    def feedback(self, app: str, run: str | None) -> Any:
        params = {"run": run} if run else {}
        return self._json(self.http.get(f"/v1/apps/{app}/feedback", params=params))

    def feedback_cases(self, app: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/feedback/cases"))

    def tasks(self, everyone: bool) -> Any:
        return self._json(self.http.get("/v1/tasks", params={"everyone": everyone}))

    def task(self, task: str) -> Any:
        return self._json(self.http.get(f"/v1/tasks/{task}"))

    def complete_task(self, task: str, output: Any, comment: str | None) -> Any:
        body = {"output": output, "comment": comment}
        return self._json(self.http.post(f"/v1/tasks/{task}/complete", json=body))

    def approvals(self, status: str, everyone: bool) -> Any:
        params: dict[str, Any] = {"status": status, "everyone": everyone}
        return self._json(self.http.get("/v1/approvals", params=params))

    def approval(self, approval: str) -> Any:
        return self._json(self.http.get(f"/v1/approvals/{approval}"))

    def answer(
        self,
        approval: str,
        verdict: str,
        comment: str | None,
        edits: dict[str, Any] | None = None,
    ) -> Any:
        body: dict[str, Any] = {"comment": comment}
        if edits:
            body["edits"] = edits
        return self._json(self.http.post(f"/v1/approvals/{approval}/{verdict}", json=body))

    def ingest(self, app: str, body: dict[str, Any]) -> Any:
        return self._json(self.http.put(f"/v1/apps/{app}/knowledge", json=body))

    def ingest_url(self, app: str, body: dict[str, Any]) -> Any:
        return self._json(self.http.post(f"/v1/apps/{app}/knowledge/url", json=body))


def _line(event: dict[str, Any]) -> str:
    data: dict[str, Any] = event.get("data") if isinstance(event.get("data"), dict) else {}  # type: ignore[assignment]
    fields = (
        "step", "reason", "capability", "tool", "provider", "model", "approval",
        "budget", "level", "action",
    )  # fmt: skip
    detail = " ".join(f"{k}={data[k]}" for k in fields if data.get(k) is not None)
    return f"{event['time'][11:23]} {event['type']:<24} {detail}".rstrip()


def _stdout() -> TextIO:
    """The current ``sys.stdout``, looked up at call time (tests redirect it)."""
    return cast(TextIO, sys.stdout)


def _unicode(out: TextIO) -> bool:
    """Whether ``out`` can show the timeline's box-drawing characters."""
    try:
        "├└│".encode(out.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def run_and_follow(client: Client, app: str, message: str, out: TextIO | None = None) -> int:
    """``aios run``: start a run, print its events as they happen, then its output."""
    out = out or _stdout()
    return follow(client, app, client.start_run(app, message), out)


def follow(client: Client, app: str, run_id: str, out: TextIO) -> int:
    """Print a run's events as they happen, then its output."""
    print(f"run {run_id}", file=out, flush=True)
    for event in client.stream(f"/v1/apps/{app}/runs/{run_id}/events"):
        print(_line(event), file=out, flush=True)  # a run can wait for a person for hours
        if event["type"] in TERMINAL:
            break
    result = client.run(app, run_id)
    print(json.dumps(result.get("output"), indent=2, ensure_ascii=not _unicode(out)), file=out)
    usage: dict[str, Any] = result.get("usage") or {}
    print(f"status={result['status']} cost=${usage.get('cost_usd', 0):.4f}", file=out)
    error: dict[str, Any] = result.get("error") or {}
    if error:
        print(f"error: {error.get('message')}", file=out)
    return 0 if result["status"] == "completed" else 1


def restart_and_follow(
    client: Client,
    app: str,
    run_id: str,
    mode: str,
    *,
    body: dict[str, Any] | None = None,
    out: TextIO | None = None,
) -> int:
    """``aios replay``, ``fork`` and ``retry``: start the new run, and follow it as ``aios run``."""
    out = out or _stdout()
    return follow(client, app, client.restart(app, run_id, mode, body or {}), out)


def _side(side: dict[str, Any]) -> str:
    note = " copied" if side["copied"] else f" {side['stubbed']} stubbed" if side["stubbed"] else ""
    return f"{side['status']} ${side['cost_usd']:.4f}{note}"


def compare(client: Client, app: str, a: str, b: str, out: TextIO | None = None) -> int:
    """``aios compare``: two runs side by side, a line per step."""
    out = out or _stdout()
    found = client.compare(app, a, b)
    print(f"{'step':<16} {'a':<22} {'b':<22} output", file=out)
    for step in found["steps"]:
        sides = [_side(step["a"]), _side(step["b"])]
        same = "same" if step["same_output"] else "DIFFERENT"
        print(f"{step['step']:<16} {sides[0]:<22} {sides[1]:<22} {same}", file=out)
    print(f"run output: {'same' if found['same_output'] else 'DIFFERENT'}", file=out)
    return 0 if found["same_output"] else 1


def trace(client: Client, app: str, run_id: str, out: TextIO | None = None) -> int:
    """``aios trace``: the run as a causal tree."""
    out = out or _stdout()
    print(render(client.events(app, run_id), color=out.isatty(), ascii=not _unicode(out)), file=out)
    return 0


def logs(client: Client, app: str, run_id: str, out: TextIO | None = None) -> int:
    """``aios logs``: the run's events in order, one line each."""
    out = out or _stdout()
    for event in client.events(app, run_id):
        print(_line(event.model_dump(mode="json")), file=out)
    return 0


def tail(client: Client, app: str | None, pattern: str, out: TextIO | None = None) -> int:
    """``aios events tail``: print matching events as they are published (polls the log)."""
    out = out or _stdout()
    import time  # noqa: PLC0415

    latest = client.log(app, pattern, 0, 1, newest=True)  # where to start, without reading it all
    after = latest[0].get("position", 0) if latest else 0
    while True:
        for event in client.log(app, pattern, after, 500):
            after = event["position"]
            print(_line(event), file=out, flush=True)
        time.sleep(0.5)
