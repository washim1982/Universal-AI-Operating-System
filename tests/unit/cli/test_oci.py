"""Apps in a registry (ADR-015; FR-APP-06), against a stand-in that keeps to the protocol."""

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.cli.oci import ARTIFACT_TYPE, BUNDLE_TYPE, MANIFEST_TYPE, Reference, Registry
from aios.controlplane.bundle import BundleError, bundle_from_documents
from aios.core.apps import Bundle

BUNDLE = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: desk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: ticket}]
  workflows: [ticket]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: ticket, version: 1.0.0}
spec:
  steps:
    - {id: only, type: wait, signal: go}
"""


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


class StandIn:
    """Enough of the Distribution API to hold what we push: blobs by digest, manifests by tag."""

    def __init__(self, password: str | None = None, *, token: bool = False) -> None:
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, bytes] = {}
        self.password = password
        self.token = token
        self.seen: list[str] = []

    def _allowed(self, request: httpx2.Request) -> bool:
        if self.password is None:
            return True
        given = request.headers.get("authorization", "")
        if self.token:
            return given == "Bearer the-token"
        pair = base64.b64encode(f"me:{self.password}".encode()).decode()
        return given == f"Basic {pair}"

    def _refused(self) -> httpx2.Response:
        if self.token:
            challenge = 'Bearer realm="https://auth.test/token",service="reg",scope="pull"'
        else:
            challenge = 'Basic realm="registry"'
        return httpx2.Response(401, headers={"www-authenticate": challenge})

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(f"{request.method} {request.url.path}")
        if request.url.host == "auth.test":
            pair = base64.b64encode(f"me:{self.password}".encode()).decode()
            if request.headers.get("authorization") != f"Basic {pair}":
                return httpx2.Response(401)
            return httpx2.Response(200, json={"token": "the-token"})
        if not self._allowed(request):
            return self._refused()
        path = request.url.path
        if path.endswith("/blobs/uploads/"):
            return httpx2.Response(202, headers={"location": f"{path}session-1?state=x"})
        if "/blobs/uploads/" in path:
            digest = request.url.params["digest"]
            if _sha256(request.content) != digest:
                return httpx2.Response(400)
            self.blobs[digest] = request.content
            return httpx2.Response(201)
        if found := re.search(r"/blobs/(sha256:\w+)$", path):
            blob = self.blobs.get(found[1])
            return httpx2.Response(404) if blob is None else httpx2.Response(200, content=blob)
        if found := re.search(r"/manifests/(.+)$", path):
            if request.method == "PUT":
                self.manifests[found[1]] = request.content
                self.manifests[_sha256(request.content)] = request.content
                return httpx2.Response(201)
            manifest = self.manifests.get(found[1])
            if manifest is None:
                return httpx2.Response(404)
            return httpx2.Response(200, content=manifest)
        return httpx2.Response(404)


REF = "registry.test/apps/desk:1.0.0"


def registry(stand_in: StandIn, reference: str = REF) -> Registry:
    http = httpx2.Client(transport=httpx2.MockTransport(stand_in))
    return Registry(Reference.parse(reference), http=http)


@pytest.fixture
def bundle() -> Bundle:
    return bundle_from_documents(BUNDLE)


def test_what_is_pushed_comes_back_the_same(bundle: Bundle) -> None:
    stand_in = StandIn()
    registry(stand_in).push(bundle)
    assert registry(stand_in).pull() == bundle


def test_it_is_a_proper_oci_artifact(bundle: Bundle) -> None:
    stand_in = StandIn()
    pinned = registry(stand_in).push(bundle)
    manifest: dict[str, Any] = json.loads(stand_in.manifests["1.0.0"])
    assert manifest["mediaType"] == MANIFEST_TYPE
    assert manifest["artifactType"] == ARTIFACT_TYPE
    assert manifest["layers"][0]["mediaType"] == BUNDLE_TYPE
    assert manifest["annotations"]["dev.aios.app.digest"] == bundle.digest
    assert pinned == _sha256(stand_in.manifests["1.0.0"])


def test_it_can_be_pinned_by_digest(bundle: Bundle) -> None:
    stand_in = StandIn()
    pinned = registry(stand_in).push(bundle)
    assert registry(stand_in, f"registry.test/apps/desk@{pinned}").pull() == bundle


def test_a_blob_already_there_isnt_sent_again(bundle: Bundle) -> None:
    stand_in = StandIn()
    registry(stand_in).push(bundle)
    stand_in.seen.clear()
    registry(stand_in, "registry.test/apps/desk:again").push(bundle)
    assert not [line for line in stand_in.seen if "uploads" in line]


def test_a_tampered_blob_is_refused(bundle: Bundle) -> None:
    stand_in = StandIn()
    registry(stand_in).push(bundle)
    [digest] = [d for d, b in stand_in.blobs.items() if len(b) > 2]
    stand_in.blobs[digest] += b" "
    with pytest.raises(BundleError, match="doesn't match its digest"):
        registry(stand_in).pull()


def test_a_blob_that_lies_about_its_app_is_refused(bundle: Bundle) -> None:
    """The registry's digests can all be right, and the bundle inside still be false."""
    stand_in = StandIn()
    registry(stand_in).push(bundle)
    forged = bundle.model_copy(update={"documents": bundle.documents + "# changed\n"})
    blob = forged.model_dump_json().encode()
    manifest = json.loads(stand_in.manifests["1.0.0"])
    manifest["layers"][0] |= {"digest": _sha256(blob), "size": len(blob)}
    stand_in.blobs[_sha256(blob)] = blob
    stand_in.manifests["1.0.0"] = json.dumps(manifest).encode()
    with pytest.raises(BundleError, match="digest"):
        registry(stand_in).pull()


def test_a_manifest_that_isnt_the_one_pinned_is_refused(bundle: Bundle) -> None:
    stand_in = StandIn()
    pinned = registry(stand_in).push(bundle)
    stand_in.manifests[pinned] = stand_in.manifests[pinned] + b" "
    with pytest.raises(BundleError, match="digest asked for"):
        registry(stand_in, f"registry.test/apps/desk@{pinned}").pull()


def test_something_else_in_the_registry_is_refused(bundle: Bundle) -> None:
    stand_in = StandIn()
    registry(stand_in).push(bundle)
    manifest = json.loads(stand_in.manifests["1.0.0"])
    manifest["artifactType"] = "application/vnd.example.other"
    stand_in.manifests["1.0.0"] = json.dumps(manifest).encode()
    with pytest.raises(BundleError, match="isn't an AIOS App"):
        registry(stand_in).pull()


def test_a_missing_version_says_so() -> None:
    with pytest.raises(BundleError, match="isn't in the registry"):
        registry(StandIn()).pull()


def test_a_private_registry_takes_a_password(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOS_REGISTRY_USERNAME", "me")
    monkeypatch.setenv("AIOS_REGISTRY_PASSWORD", "open-up")
    stand_in = StandIn("open-up")
    registry(stand_in).push(bundle)
    assert registry(stand_in).pull() == bundle


def test_a_registry_can_want_a_token_instead(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The way GHCR, Harbor and Docker Hub do it."""
    monkeypatch.setenv("AIOS_REGISTRY_USERNAME", "me")
    monkeypatch.setenv("AIOS_REGISTRY_PASSWORD", "open-up")
    stand_in = StandIn("open-up", token=True)
    registry(stand_in).push(bundle)
    assert registry(stand_in).pull() == bundle


def test_without_a_password_a_private_registry_refuses(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIOS_REGISTRY_USERNAME", raising=False)
    monkeypatch.delenv("AIOS_REGISTRY_PASSWORD", raising=False)
    with pytest.raises(httpx2.HTTPStatusError):
        registry(StandIn("open-up")).push(bundle)


@pytest.mark.parametrize(
    ("text", "base", "reference"),
    [
        ("ghcr.io/acme/desk:1.0.0", "https://ghcr.io/v2/acme/desk", "1.0.0"),
        ("127.0.0.1:5000/desk:2", "http://127.0.0.1:5000/v2/desk", "2"),
        ("localhost:5000/a/b:c", "http://localhost:5000/v2/a/b", "c"),
    ],
)
def test_references_read_as_they_look(text: str, base: str, reference: str) -> None:
    parsed = Reference.parse(text)
    assert (parsed.base, parsed.reference) == (base, reference)


def test_only_a_local_registry_goes_without_tls() -> None:
    assert Reference.parse("registry.internal:5000/desk:1").base.startswith("https://")


@pytest.mark.parametrize("text", ["desk", "desk:1.0.0", "ghcr.io/desk", "ghcr.io/Desk:1"])
def test_a_reference_needs_a_registry_a_name_and_a_tag(text: str) -> None:
    with pytest.raises(BundleError, match="isn't a reference"):
        Reference.parse(text)


def test_a_directory_can_be_packaged_and_read_back(tmp_path: Path) -> None:
    from aios.cli.apps import _save, _source  # noqa: PLC0415

    bundle = bundle_from_documents(BUNDLE)
    target = tmp_path / "saved.json"
    assert _save(bundle, target) == {"file": str(target), "digest": bundle.digest}
    assert _source(str(target)) == bundle
