# ADR-015: An App version is one JSON bundle, kept as an OCI artifact in any registry

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-02
- **Deciders:** Tech lead
- **Requirements:** FR-APP-06 (P1); WP 3.1

## Context

FR-APP-06 asks for Apps packaged as OCI artifacts and published to a private registry. Until now an
App was a folder that a server was started with (`--app`), or manifests sent to `/v1/apply`. Neither
gave a version that someone could review once and then trust everywhere: the folder could change
under a running server, and `/v1/apply` carried no code.

Installation (ADR-019) needs a version to be one thing with one name. A tenant admin agrees to
what a version asks for, and that agreement only means something if the version can't change
afterwards.

## Decision

**A version is a bundle:** name, version, its manifests and policies as text with `${config.*}`
left unfilled, its configuration schema, and its `src/` folder as a gzipped tar in base64. Its
`digest` is SHA-256 over all of that. `verified()` recomputes it, so a bundle that has been changed
anywhere is refused wherever it arrives. `aios app package` writes one as a file.

**Packing is reproducible.** Files go in sorted order with fixed modes and no timestamps, compiled
files are left out, and gzip's own time field is zero. The same folder gives the same digest on any
machine on any day, which is what makes a digest worth reviewing.

**In a registry, an App is an OCI artifact of one layer.** The manifest's `artifactType` is
`application/vnd.aios.app.v1`, its one layer is the bundle (`application/vnd.aios.app.bundle.v1+json`),
and its config is the empty descriptor, as the OCI 1.1 guidance for artifacts says. Annotations name
the App, its version and the bundle's digest. Any registry that speaks the Distribution API will
hold it: Harbor, GHCR, ECR, or `registry:2` in the compose stack's `registry` profile.

**Pulling trusts nothing until it has checked it.** A reference pinned by digest
(`registry/name@sha256:…`) must match the manifest that comes back; the blob must match the
manifest's digest for it; the artifact type must be ours; and the bundle must pass `verified()`.

**The CLI talks to the registry, and the server never does.** `aios app push`, `pull` and `install
<reference>` run on the operator's machine, which uploads the bundle to `POST /v1/installations`.
The server has no registry credentials and no egress for them. It keeps the bundles it was given in
its database (ADR-019), so a worker can start a version without a registry being up.

**Credentials come from `AIOS_REGISTRY_USERNAME` and `AIOS_REGISTRY_PASSWORD`**, as Basic, or
exchanged for a token when the registry asks for one (the way GHCR, Harbor and Docker Hub do it).
A registry on `127.0.0.1` or `localhost` is reached over HTTP; everything else over HTTPS.

**A worker unpacks a version's code into a folder named after its digest**, in `AIOS_APP_CACHE`
(default `.aios/apps`). Each process unpacks into a folder of its own and renames it into place, so
an API and a worker on one machine can't collide, and a tar entry can't climb out of the folder.

## Alternatives considered

- **A container image per App.** The obvious OCI artifact, and the right one once agents run in
  their own processes (ADR-014). Today agents run in the worker's process, so an image would carry
  an interpreter nobody runs.
- **A layer per file, or the code as its own layer.** Lets a registry share layers between versions.
  Bundles are small, and one layer keeps "the digest" a single thing to review.
- **ORAS or a registry client library.** One more dependency for four HTTP calls.
- **The server pulls from the registry.** Simpler for the operator, but every API process would
  need credentials and egress to the registry, for something a person does a few times a week.
- **Signatures (Sigstore, Notation).** A digest proves the bundle is the one reviewed, not who
  made it. Signing is worth having before a public marketplace (Phase 5), and fits on top of this.

## Consequences

- A version can be installed from a folder, a saved file or a registry, and in each case it is the
  same digest. `/v1/apply` makes a bundle with no code from the manifests it is sent.
- A registry is a store, not an authority. Trust comes from the digest a person agreed to.
- The code in a bundle is imported into a shared worker process, so offering one still needs
  `platform-admin` (ADR-011). Isolation is ADR-014.
- A bundle is held in the database whole. Large Apps, with models or data files, should keep those
  elsewhere.
- Unpacked code folders are never removed. A version's folder is small and may be needed again by
  a rollback; cleaning up is left to the operator for now.

## Implementation

`src/aios/controlplane/bundle.py` (`bundle_of`, `verified`, `code_folder`), `src/aios/cli/oci.py`,
`src/aios/cli/apps.py`; `tests/unit/controlplane/test_bundle.py`, `tests/unit/cli/test_oci.py`,
`tests/unit/cli/test_app_commands.py`. A real `registry:2.8.3` is checked by the testing guide,
Part 16.
