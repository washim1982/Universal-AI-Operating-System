"""CLI commands that talk to a running AIOS API (WP 1.7; FR-CP-03)."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO, cast

import httpx2

from aios.core.envelope import Event
from aios.observability import render

TERMINAL = ("run.completed", "run.failed", "run.cancelled")


class Client:
    def __init__(self, server: str, tenant: str | None = None, timeout: float = 30.0) -> None:
        headers = {"X-AIOS-Tenant": tenant} if tenant else {}
        self.http = httpx2.Client(base_url=server.rstrip("/"), headers=headers, timeout=timeout)

    def _json(self, response: httpx2.Response) -> Any:
        if response.status_code >= 400:
            detail = (
                response.json().get("detail")
                if response.headers.get("content-type", "").startswith("application/json")
                else response.text
            )
            raise SystemExit(f"error {response.status_code}: {detail}")
        return response.json() if response.content else None

    def apply(self, path: Path) -> list[Any]:
        files = sorted(path.rglob("*.yaml")) if path.is_dir() else [path]
        text = "\n---\n".join(f.read_text(encoding="utf-8") for f in files)
        response = self.http.post(
            "/v1/apply", content=text, headers={"Content-Type": "application/yaml"}
        )
        return [self._json(response)]

    def apps(self) -> Any:
        return self._json(self.http.get("/v1/apps"))

    def start_run(self, app: str, message: str) -> str:
        return str(
            self._json(self.http.post(f"/v1/apps/{app}/runs", json={"message": message}))["run_id"]
        )

    def run(self, app: str, run_id: str) -> Any:
        return self._json(self.http.get(f"/v1/apps/{app}/runs/{run_id}"))

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

    def dlq(self) -> Any:
        return self._json(self.http.get("/v1/dlq"))

    def redrive(self, letter: str) -> Any:
        return self._json(self.http.post(f"/v1/dlq/{letter}/redrive"))

    def discard(self, letter: str) -> None:
        self._json(self.http.delete(f"/v1/dlq/{letter}"))


def _line(event: dict[str, Any]) -> str:
    data: dict[str, Any] = event.get("data") if isinstance(event.get("data"), dict) else {}  # type: ignore[assignment]
    detail = " ".join(
        f"{k}={data[k]}" for k in ("step", "capability", "tool", "provider", "model") if k in data
    )
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
    run_id = client.start_run(app, message)
    print(f"run {run_id}", file=out)
    for event in client.stream(f"/v1/apps/{app}/runs/{run_id}/events"):
        print(_line(event), file=out)
        if event["type"] in TERMINAL:
            break
    result = client.run(app, run_id)
    print(json.dumps(result.get("output"), indent=2, ensure_ascii=False), file=out)
    usage: dict[str, Any] = result.get("usage") or {}
    print(f"status={result['status']} cost=${usage.get('cost_usd', 0):.4f}", file=out)
    return 0 if result["status"] == "completed" else 1


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

    after = max((e.get("position", 0) for e in client.http.get(
        "/v1/events", params={"app": app, "pattern": pattern, "after": 0, "limit": 100000}
    ).json()), default=0)  # fmt: skip
    while True:
        response = client.http.get(
            "/v1/events", params={"app": app, "pattern": pattern, "after": after}
        )
        for event in response.json():
            after = event["position"]
            print(_line(event), file=out, flush=True)
        time.sleep(0.5)
