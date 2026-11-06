"""Bundles (ADR-015): the same App always gives the same digest, and a digest can't be faked."""

import base64
import gzip
import io
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path

import pytest

from aios.controlplane.bundle import (
    BundleError,
    bundle_from_documents,
    bundle_of,
    check,
    code_folder,
    filled_in,
    rendered,
    requested_by,
    verified,
)
from aios.core.apps import Bundle
from aios.core.envelope import DataClass

APP = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: ticket}]
  agents: [worker]
  workflows: [ticket]
  permissions: {tools: ["mail.>"], dataClasses: [internal, confidential]}
  memory: {scopes: [session]}
  egress: {allow: [docs.example.com], tools: {"web.page.read": [wiki.example.com]}}
  budgets: {perRunUSD: 0.5, monthlyUSD: 100}
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: worker, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "desk.agents:work"}
  capabilities: [{name: desk.work}]
  tools: {allow: ["pay.refund.create"]}
  memory: {read: [app], write: [user]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: ticket, version: 1.0.0}
spec:
  steps:
    - {id: only, type: task, capability: desk.work}
    - {id: note, type: tool, tool: audit.note.write}
"""
SCHEMA = '{"type": "object", "properties": {"limit": {"type": "number", "default": 50}}}'


def _write(root: Path, *, code: str = "VALUE = 1\n", manifests: str = APP) -> Path:
    (root / "manifests").mkdir(parents=True)
    (root / "manifests" / "app.yaml").write_text(manifests, "utf-8")
    package = root / "src" / "desk_code"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(code, "utf-8")
    return root


def test_an_app_becomes_a_bundle(tmp_path: Path) -> None:
    bundle = bundle_of(_write(tmp_path))
    assert (bundle.name, bundle.version) == ("desk", "1.0.0")
    assert bundle.digest.startswith("sha256:")
    assert "kind: App" in bundle.documents
    assert bundle.code is not None


def test_policies_and_the_schema_go_with_it(tmp_path: Path) -> None:
    root = _write(tmp_path)
    (root / "policies").mkdir()
    (root / "policies" / "rules.yaml").write_text(
        "apiVersion: aios/v1alpha1\nkind: Policy\nmetadata: {name: rules, version: 1.0.0}\n"
        'spec: {subject: {app: desk}, allow: [{action: tool.invoke, resource: "mail.>"}]}\n',
        "utf-8",
    )
    (root / "config.schema.json").write_text(SCHEMA, "utf-8")
    bundle = bundle_of(root)
    assert "kind: Policy" in bundle.documents
    assert bundle.config_schema is not None


def test_an_app_with_no_code_has_none(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "manifests").mkdir(parents=True)
    (root / "manifests" / "app.yaml").write_text(APP, "utf-8")
    assert bundle_of(root).code is None


def test_a_folder_that_isnt_an_app_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="not an App directory"):
        bundle_of(tmp_path)


def test_the_same_app_gives_the_same_digest(tmp_path: Path) -> None:
    """Wherever and whenever it is packed. That's what makes a digest worth reviewing."""
    one = bundle_of(_write(tmp_path / "one"))
    two = bundle_of(_write(tmp_path / "two"))
    assert one.digest == two.digest
    assert one.code == two.code


def test_the_digest_doesnt_depend_on_when(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _write(tmp_path)
    before = bundle_of(root).digest
    monkeypatch.setattr(time, "time", lambda: 2_000_000_000.0)
    assert bundle_of(root).digest == before


def test_compiled_files_dont_count(tmp_path: Path) -> None:
    root = _write(tmp_path)
    before = bundle_of(root).digest
    cache = root / "src" / "desk_code" / "__pycache__"
    cache.mkdir()
    (cache / "x.cpython-314.pyc").write_bytes(b"compiled")
    assert bundle_of(root).digest == before


def test_a_change_of_code_changes_the_digest(tmp_path: Path) -> None:
    one = bundle_of(_write(tmp_path / "one"))
    two = bundle_of(_write(tmp_path / "two", code="VALUE = 2\n"))
    assert one.digest != two.digest


def test_a_change_of_manifest_changes_the_digest(tmp_path: Path) -> None:
    one = bundle_of(_write(tmp_path / "one"))
    two = bundle_of(_write(tmp_path / "two", manifests=APP.replace("0.5", "0.6")))
    assert one.digest != two.digest


def test_a_true_bundle_passes(tmp_path: Path) -> None:
    bundle = bundle_of(_write(tmp_path))
    assert verified(bundle) is bundle


@pytest.mark.parametrize(
    "change",
    [
        {"documents": "kind: App\n# tampered"},
        {"code": "dGFtcGVyZWQ="},
        {"config_schema": {"type": "string"}},
        {"name": "other"},
        {"version": "9.9.9"},
    ],
)
def test_a_tampered_bundle_is_refused(tmp_path: Path, change: dict[str, object]) -> None:
    bundle = bundle_of(_write(tmp_path)).model_copy(update=change)
    with pytest.raises(BundleError, match="digest"):
        verified(bundle)


def test_manifests_alone_make_a_version() -> None:
    bundle = bundle_from_documents(APP)
    assert (bundle.name, bundle.version, bundle.code) == ("desk", "1.0.0", None)
    assert verified(bundle) is bundle


def test_configuration_takes_its_defaults() -> None:
    schema = {"type": "object", "properties": {"limit": {"type": "number", "default": 50}}}
    assert filled_in(schema, None) == {"limit": 50}
    assert filled_in(schema, {"limit": 75}) == {"limit": 75}


def test_configuration_is_checked_against_the_schema() -> None:
    schema = {"type": "object", "properties": {"limit": {"type": "number"}}}
    with pytest.raises(BundleError, match="invalid configuration"):
        filled_in(schema, {"limit": "lots"})


def test_a_manifest_that_doesnt_parse_is_refused() -> None:
    with pytest.raises(BundleError):
        rendered("kind: Nonsense\n", None, None)


def test_an_app_needs_what_it_names() -> None:
    manifests = rendered(APP.split("---", maxsplit=1)[0], None, None)
    with pytest.raises(BundleError, match="no manifest for Agent worker, Workflow ticket"):
        check(manifests)


def test_there_must_be_one_app() -> None:
    with pytest.raises(BundleError, match="exactly one App manifest"):
        check(rendered(APP.split("---")[1], None, None))


def test_an_app_cant_ask_quietly() -> None:
    """What its agents and steps could do counts, whatever ``permissions`` says."""
    requested = requested_by(rendered(APP, None, None))
    assert requested.tools == ["audit.note.write", "mail.>", "pay.refund.create"]
    assert requested.memory_scopes == ["app", "session", "user"]
    assert requested.data_classes == [DataClass.CONFIDENTIAL, DataClass.INTERNAL]
    assert requested.hosts == ["docs.example.com", "wiki.example.com"]
    assert (requested.per_run_usd, requested.monthly_usd) == (0.5, 100)


def _with_code(tmp_path: Path, code: str) -> Bundle:
    return bundle_of(_write(tmp_path, code=code))


def test_code_unpacks_and_can_be_imported(tmp_path: Path) -> None:
    bundle = _with_code(tmp_path / "app", "VALUE = 'first'\n")
    target = code_folder(bundle, tmp_path / "cache")
    assert target is not None
    assert (target / "desk_code" / "__init__.py").exists()
    desk_code = import_module("desk_code")
    assert desk_code.VALUE == "first"
    sys.path.remove(str(target))
    del sys.modules["desk_code"]


def test_a_new_version_really_loads(tmp_path: Path) -> None:
    """Python keeps a module once imported, and would go on running the old version for ever."""
    cache = tmp_path / "cache"
    first = code_folder(_with_code(tmp_path / "one", "VALUE = 'first'\n"), cache)
    desk_code = import_module("desk_code")
    assert desk_code.VALUE == "first"
    second = code_folder(_with_code(tmp_path / "two", "VALUE = 'second'\n"), cache)
    again = import_module("desk_code")
    assert again.VALUE == "second"
    for folder in (first, second):
        sys.path.remove(str(folder))
    del sys.modules["desk_code"]


def test_going_back_finds_the_old_code_again(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    old = _with_code(tmp_path / "one", "VALUE = 'first'\n")
    new = _with_code(tmp_path / "two", "VALUE = 'second'\n")
    folders = [code_folder(old, cache), code_folder(new, cache), code_folder(old, cache)]
    desk_code = import_module("desk_code")
    assert desk_code.VALUE == "first"
    for folder in {str(f) for f in folders}:
        sys.path.remove(folder)
    del sys.modules["desk_code"]


def test_two_processes_can_unpack_one_version(tmp_path: Path) -> None:
    """An API and a worker on one machine share a cache, and both start at once."""
    bundle = _with_code(tmp_path / "app", "VALUE = 'first'\n")
    cache = tmp_path / "cache"
    with ThreadPoolExecutor(8) as pool:
        folders = set(pool.map(code_folder, [bundle] * 8, [cache] * 8))
    [folder] = folders
    assert folder is not None
    assert [path.name for path in cache.iterdir()] == [folder.name]
    sys.path[:] = [entry for entry in sys.path if entry != str(folder)]
    sys.modules.pop("desk_code", None)


def test_no_code_means_nothing_to_unpack(tmp_path: Path) -> None:
    assert code_folder(bundle_from_documents(APP), tmp_path) is None


def test_code_cant_climb_out_of_its_folder(tmp_path: Path) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("../../escaped.py")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    code = base64.b64encode(gzip.compress(raw.getvalue())).decode()
    bundle = Bundle(name="evil", version="1", digest="sha256:evil", documents="", code=code)
    with pytest.raises(tarfile.FilterError):
        code_folder(bundle, tmp_path / "deep" / "cache")
    assert not (tmp_path / "deep" / "escaped.py").exists()
