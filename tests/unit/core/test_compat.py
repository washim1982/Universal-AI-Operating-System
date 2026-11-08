"""Schemas that still read what was written before (FR-EVT-10; WP 3.6)."""

import json
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
from pydantic import BaseModel

from aios.api import create_app
from aios.cli.main import main
from aios.core.compat import breaking_changes, compare_folders
from aios.core.schemas import render_specs
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


class Before(BaseModel):
    approval: str
    risk: Literal["low", "high"]
    comment: str | None = None
    count: int = 0


class AddsOptional(Before):
    note: str | None = None


class Widens(BaseModel):
    approval: str
    risk: Literal["low", "medium", "high"]
    comment: str | int | None = None
    count: float = 0


class Narrows(BaseModel):
    approval: str
    risk: Literal["low"]
    comment: str
    count: int = 0


class Drops(BaseModel):
    model_config = {"extra": "forbid"}

    approval: str
    risk: Literal["low", "high"]


def test_what_adds_or_widens_breaks_nobody() -> None:
    assert breaking_changes(_schema(Before), _schema(AddsOptional)) == []
    assert breaking_changes(_schema(Before), _schema(Widens)) == []


def test_what_narrows_breaks_readers_of_old_events() -> None:
    assert breaking_changes(_schema(Before), _schema(Narrows)) == [
        "comment: no longer takes null",
        'risk: no longer takes "high"',
        "comment: now required",
    ]


def test_what_drops_a_field_breaks_them_too() -> None:
    assert breaking_changes(_schema(Before), _schema(Drops)) == [
        "comment: removed",
        "count: removed",
        "the document: no longer takes other fields",
    ]


def test_nested_models_are_compared_by_name() -> None:
    class Usage(BaseModel):
        tokens: int
        cost: float | None = None

    class Call(BaseModel):
        usage: Usage

    class StricterUsage(BaseModel):
        tokens: int
        cost: float

    class StricterCall(BaseModel):
        usage: StricterUsage

    old, new = _schema(Call), _schema(StricterCall)
    new["$defs"] = {"Usage": new["$defs"].pop("StricterUsage")}
    new["properties"]["usage"] = {"$ref": "#/$defs/Usage"}
    assert breaking_changes(old, new) == [
        "Usage.cost: no longer takes null",
        "Usage.cost: now required",
    ]


def test_a_removed_event_type_is_breaking(tmp_path: Path) -> None:
    current = render_specs()
    old = tmp_path / "specs" / "schemas" / "events"
    old.mkdir(parents=True)
    kept = "schemas/events/approval.granted.v1.json"
    (old / "approval.granted.v1.json").write_text(current[kept], encoding="utf-8")
    (old / "refund.issued.v1.json").write_text(json.dumps({"type": "object"}), encoding="utf-8")
    assert compare_folders(tmp_path / "specs", current) == ["events/refund.issued.v1: removed"]


def test_the_committed_specs_read_with_todays_models() -> None:
    assert compare_folders(ROOT / "specs", render_specs()) == []


def test_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["specs", "compat", "--against", str(ROOT / "specs")]) == 0
    old = tmp_path / "specs" / "schemas" / "events"
    old.mkdir(parents=True)
    (old / "gone.v1.json").write_text("{}", encoding="utf-8")
    assert main(["specs", "compat", "--against", str(tmp_path / "specs")]) == 1
    assert "breaking: events/gone.v1: removed" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="no schemas/ folder"):
        main(["specs", "compat", "--against", str(tmp_path)])


async def test_the_registry_serves_every_schema() -> None:
    async with LocalRuntime(tenant="acme") as runtime:
        transport = httpx2.ASGITransport(app=create_app(runtime.runtime))
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            found = (await http.get("/v1/schemas")).json()
            granted = (await http.get("/v1/schemas/events/approval.granted")).json()
            app = (await http.get("/v1/schemas/manifests/app")).json()
            envelope = (await http.get("/v1/schemas/envelope")).json()
            missing = await http.get("/v1/schemas/events/nothing.like.this")
            pending = await http.get("/v1/schemas/manifests/robot")
    entry = next(e for e in found["events"] if e["type"] == "approval.granted")
    assert entry["schema"] == "/v1/schemas/events/approval.granted"
    assert granted == json.loads(render_specs()["schemas/events/approval.granted.v1.json"])
    assert "humans" in app["$defs"]["AppSpec"]["properties"]
    assert envelope["$id"] == "aios://schemas/envelope/1"
    assert [m["kind"] for m in found["manifests"]] == [
        "agent",
        "app",
        "workflow",
        "policy",
        "model-policy",
        "evaluation",
    ]
    assert (missing.status_code, pending.status_code) == (404, 404)
