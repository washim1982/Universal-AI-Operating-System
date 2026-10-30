# ADR-012: License and open-core boundary

- **Status:** Accepted — 29 Oct 2026, project owner (requirements Q1, WP 0.4)
- **Date:** 2026-10-28 (proposed), 2026-10-29 (decided)
- **Deciders:** Project owner
- **Requirements:** Product strategy (requirements §9); risk R3

## Context

The go-to-market plan uses AIOS Core for developer adoption and the Enterprise Runtime as the
revenue engine. The license decides who adopts, who contributes, and what can be sold. It must
be settled before the repository is public or outside contributions arrive.

## Decision

**Apache-2.0 core, commercial enterprise edition.**

- **Apache-2.0** for AIOS Core: runtime, fabric adapters, SDKs, CLI, the conformance suites,
  the Agent / App / Workflow / Policy specs and the reference apps. Apache-2.0's patent grant
  matters to the enterprise buyers we target, and it matches the ecosystem we integrate with.
- **Commercial license** for the Enterprise edition, in a separate repository and package
  (`aios-enterprise`): SSO/SCIM, advanced ABAC, audit export to SIEM, data residency, HA
  operator features, private-LLM packs, compliance reports, support.
- **Boundary rule:** nothing needed to run a single-tenant production deployment safely
  (policy enforcement, audit chain verification, approvals, isolation) is held back for the
  enterprise edition. Enterprise features are about scale, integration and compliance.
  [docs/editions.md](../editions.md) lists both editions and how Enterprise plugs in through
  public ports.
- **Contributions** under the Developer Certificate of Origin (sign-off), with no CLA. Because
  the Enterprise edition is a separate codebase, Core never needs to be relicensed, which is
  the main reason a CLA would have been worth its friction.
- **Dependencies** of Core use permissive licenses compatible with Apache-2.0; a copyleft
  dependency needs an ADR.

## Alternatives considered

| Option | Trade-off |
|---|---|
| BSL / source-available core | Protects against hosting competitors; slows adoption and contributions |
| AGPL core | Strong copyleft deters many enterprises from embedding |
| Fully proprietary | Loses the developer-adoption wedge that the strategy depends on |
| CLA instead of DCO | Allows relicensing Core later; adds contributor friction we don't need with a separate Enterprise codebase |

## Consequences

- A hosting competitor may offer AIOS Core as a service; the Enterprise features, support and
  the managed offerings are the response, not the license.
- Enterprise code may only use public extension points, so every extension point stays
  documented and available to third parties too.
- The product name is still a working name: the trademark check (requirements Q7, risk R12)
  must finish before public branding. A rename changes `NOTICE`, package names and docs, not
  the license.

## Implementation

`LICENSE` (Apache License 2.0, verbatim), `NOTICE`, `license = "Apache-2.0"` and
`license-files` in `pyproject.toml`, OCI license label in the `Dockerfile`, `CONTRIBUTING.md`
(DCO), the `dco` CI job (`scripts/check-dco.sh`), [docs/editions.md](../editions.md), and
`tests/unit/test_licenses.py` (package metadata and dependency licenses).
