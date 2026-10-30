# Editions: AIOS Core and AIOS Enterprise

Decided in [ADR-012](adr/ADR-012-license.md) (29 Oct 2026).

| | **AIOS Core** | **AIOS Enterprise** |
|---|---|---|
| License | [Apache-2.0](../LICENSE) | Commercial |
| Where | This repository, package `aios` | A separate private repository, package `aios-enterprise` |
| Who it is for | Developers and any team running AIOS in production | Regulated organizations that need scale, integration and compliance |

## The boundary rule

**Nothing needed to run a single-tenant production deployment safely is held back for the
Enterprise edition.** Enterprise features are about scale, integration with enterprise
systems, and compliance evidence. When a feature could go either way, it goes into Core.

## What is in each edition

| Area | Core (Apache-2.0) | Enterprise (commercial) |
|---|---|---|
| Runtime | Event Fabric (in-memory, PostgreSQL, NATS adapters), Agent Kernel, Workflow Engine, Model Kernel, Tool OS, Memory OS, Human OS | — |
| Security | Default-deny policy enforcement with Cedar (ADR-007), RBAC and attribute conditions, delegation (agent ∩ user), taint tracking and injection defenses, approvals, tenant isolation, secrets encryption, hash-chained audit log with `aios audit verify` | Advanced ABAC: attributes sourced from the IdP |
| Identity | OIDC sign-in, API keys, agent workload identities | SAML and SCIM provisioning (FR-SEC-02 P2), SSO hardening |
| Audit and compliance | Audit log, verification, query API | Audit export to SIEM (FR-SEC-08 P1), SOC 2 / HIPAA / EU AI Act evidence reports |
| Data | Multi-tenancy on one installation, GDPR erasure | Per-tenant data residency (NFR-09) |
| Models | Every provider adapter, OpenAI-compatible endpoints for self-hosted models, model-routing policies | Private-LLM packs: validated self-hosted model bundles |
| Operations | Docker Compose, Kubernetes operator, OpenTelemetry traces, metrics, replay | HA and multi-region operator features |
| Developer tools | CLI, SDKs, specs, conformance suites, reference apps | — |
| Support | Community | Support with SLAs |

Hosted offerings (the managed AI Workflow Cloud, managed SaaS) are services built on these
editions, not editions themselves. Items move from Enterprise to Core when they become necessary to run safely. They never move
the other way.

## How the Enterprise edition plugs in

- Enterprise components implement the **same public ports** that Core defines (for example
  `PolicyDecisionPoint`, the `EventBus`, model providers, audit sinks and identity providers)
  and must pass the same conformance suites.
- Core **never imports** Enterprise code and has no hidden hooks: every extension point is
  public and documented, so third parties can build on it too.
- The Enterprise package depends on released versions of `aios` and ships on its own release
  train.

## Dependencies and contributions

- Core's runtime dependencies use permissive licenses compatible with Apache-2.0 (MIT, BSD,
  Apache-2.0, ISC, PSF and similar). MPL-2.0 is also allowed for dependencies used unmodified:
  its copyleft applies only to changes in the MPL files themselves (`certifi`, which every
  Python HTTP client uses, is MPL-2.0). `tests/unit/test_licenses.py` enforces this. A
  copyleft dependency (GPL, AGPL, LGPL) needs an ADR first. For example, the PostgreSQL
  adapter uses `asyncpg` (Apache-2.0), not `psycopg` (LGPL-3.0).
- Contributions are accepted under the Developer Certificate of Origin, with no CLA
  ([CONTRIBUTING.md](../CONTRIBUTING.md)). The Enterprise edition lives in another repository,
  so Core never needs to be relicensed.
- The product name is a working name until the trademark check (requirements Q7) is done.
