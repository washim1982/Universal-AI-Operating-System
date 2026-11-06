"""Apps as OCI artifacts in a private registry (ADR-015, FR-APP-06).

Any registry that speaks the OCI Distribution API will hold an App: Harbor, GHCR, ECR, or
``registry:2`` in the compose stack. An artifact is one blob, the bundle as JSON, under an
``artifactType`` of our own, so a registry stores it without needing to know what it is. Pulling
checks every digest on the way: the blob's, and the bundle's against its contents.

``AIOS_REGISTRY_USERNAME`` and ``AIOS_REGISTRY_PASSWORD`` give a registry its credentials.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx2

from aios.controlplane.bundle import BundleError, verified
from aios.core.apps import Bundle

MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
ARTIFACT_TYPE = "application/vnd.aios.app.v1"
BUNDLE_TYPE = "application/vnd.aios.app.bundle.v1+json"
EMPTY_TYPE = "application/vnd.oci.empty.v1+json"
EMPTY = b"{}"
_REFERENCE = re.compile(
    r"^(?P<registry>[a-zA-Z0-9.-]+(:\d+)?)/(?P<repository>[a-z0-9._/-]+)"
    r"(:(?P<tag>[\w.-]+))?(@(?P<digest>sha256:[0-9a-f]{64}))?$"
)
_LOCAL = ("localhost", "127.0.0.1")


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


@dataclass(frozen=True, slots=True)
class Reference:
    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @classmethod
    def parse(cls, text: str) -> Reference:
        found = _REFERENCE.match(text)
        if found is None or (found["tag"] is None and found["digest"] is None):
            raise BundleError(f"{text!r} isn't a reference like registry/name:tag")
        return cls(found["registry"], found["repository"], found["tag"], found["digest"])

    @property
    def base(self) -> str:
        scheme = "http" if self.registry.split(":")[0] in _LOCAL else "https"
        return f"{scheme}://{self.registry}/v2/{self.repository}"

    @property
    def reference(self) -> str:
        return self.digest or str(self.tag)


def _credentials() -> tuple[str, str] | None:
    username = os.environ.get("AIOS_REGISTRY_USERNAME")
    password = os.environ.get("AIOS_REGISTRY_PASSWORD")
    return (username, password) if username and password else None


class Registry:
    def __init__(self, reference: Reference, *, http: httpx2.Client | None = None) -> None:
        self.ref = reference
        self.http = http or httpx2.Client(timeout=60.0, follow_redirects=True)
        self._token: str | None = None

    def _request(self, method: str, url: str, **options: Any) -> httpx2.Response:
        headers = dict(options.pop("headers", {}))
        response = self.http.request(method, url, headers=self._auth() | headers, **options)
        if response.status_code == 401 and self._token is None:
            self._sign_in(response.headers.get("www-authenticate", ""))
            response = self.http.request(method, url, headers=self._auth() | headers, **options)
        return response

    def _auth(self) -> dict[str, str]:
        if self._token is not None:
            return {"Authorization": f"Bearer {self._token}"}
        known = _credentials()
        if known is None:
            return {}
        pair = base64.b64encode(":".join(known).encode()).decode()
        return {"Authorization": f"Basic {pair}"}

    def _sign_in(self, challenge: str) -> None:
        """The token dance GHCR, Harbor and Docker Hub use, if the registry asked for it."""
        if not challenge.lower().startswith("bearer "):
            return
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = fields.pop("realm", None)
        if realm is None:
            return
        answer = self.http.get(realm, params=fields, auth=_credentials())
        if answer.status_code == 200:
            body = answer.json()
            self._token = body.get("token") or body.get("access_token")

    def _blob_url(self, digest: str) -> str:
        return f"{self.ref.base}/blobs/{digest}"

    def _push_blob(self, data: bytes) -> str:
        digest = _sha256(data)
        if self._request("HEAD", self._blob_url(digest)).status_code == 200:
            return digest
        started = self._request("POST", f"{self.ref.base}/blobs/uploads/")
        started.raise_for_status()
        location = urljoin(f"{self.ref.base}/", started.headers["location"])
        joiner = "&" if "?" in location else "?"
        finished = self._request(
            "PUT",
            f"{location}{joiner}digest={digest}",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        finished.raise_for_status()
        return digest

    def push(self, bundle: Bundle) -> str:
        """Push a version, and return the digest that pins it."""
        verified(bundle)
        blob = bundle.model_dump_json().encode()
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": MANIFEST_TYPE,
                "artifactType": ARTIFACT_TYPE,
                "config": {
                    "mediaType": EMPTY_TYPE,
                    "digest": self._push_blob(EMPTY),
                    "size": len(EMPTY),
                },
                "layers": [
                    {
                        "mediaType": BUNDLE_TYPE,
                        "digest": self._push_blob(blob),
                        "size": len(blob),
                    }
                ],
                "annotations": {
                    "org.opencontainers.image.title": bundle.name,
                    "org.opencontainers.image.version": bundle.version,
                    "dev.aios.app.digest": bundle.digest,
                },
            },
            sort_keys=True,
        ).encode()
        response = self._request(
            "PUT",
            f"{self.ref.base}/manifests/{self.ref.reference}",
            content=manifest,
            headers={"Content-Type": MANIFEST_TYPE},
        )
        response.raise_for_status()
        return _sha256(manifest)

    def pull(self) -> Bundle:
        """Fetch a version, trusting nothing until every digest has been checked."""
        response = self._request(
            "GET",
            f"{self.ref.base}/manifests/{self.ref.reference}",
            headers={"Accept": MANIFEST_TYPE},
        )
        if response.status_code == 404:
            raise BundleError(f"{self.ref.repository}:{self.ref.reference} isn't in the registry")
        response.raise_for_status()
        if self.ref.digest is not None and _sha256(response.content) != self.ref.digest:
            raise BundleError("the registry's manifest doesn't match the digest asked for")
        manifest: dict[str, Any] = response.json()
        if manifest.get("artifactType") != ARTIFACT_TYPE:
            raise BundleError(f"{self.ref.repository} isn't an AIOS App")
        [layer] = manifest["layers"]
        blob = self._request("GET", self._blob_url(layer["digest"]))
        blob.raise_for_status()
        if _sha256(blob.content) != layer["digest"]:
            raise BundleError("the registry's blob doesn't match its digest")
        return verified(Bundle.model_validate_json(blob.content))
