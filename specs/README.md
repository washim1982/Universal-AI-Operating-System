# AIOS specifications v0.1

The contracts other code and other languages depend on. The Pydantic models in `src/aios/core`
(and the Cedar schema in `src/aios/security/cedar_schema.py`) are the source of truth; files
marked *generated* are written by `aios specs export`, and CI fails (`aios specs check`) when
they are stale.

| Spec | File | Status |
|---|---|---|
| Event envelope | [event-envelope.md](event-envelope.md), [schemas/envelope.v1.json](schemas/envelope.v1.json) (generated) | v0.1 draft — ADR-002 |
| Topic grammar | [topic-grammar.md](topic-grammar.md) | v0.1 draft — ADR-003 |
| Standard event catalog | [event-catalog.md](event-catalog.md), [schemas/events/](schemas/events/) (generated) | v0.1 draft |
| Agent, App, Workflow, Policy, ModelPolicy manifests | [manifests.md](manifests.md), [schemas/manifests/](schemas/manifests/) (generated) | `aios/v1alpha1` |
| Cedar schema for compiled policies | [policy/aios.cedarschema](policy/aios.cedarschema) (generated) | v0.1 — ADR-007 |

## Versioning rules

- **Event types** carry a schema version in `dataschema` (`aios://schemas/<type>/<n>`). Within
  a version, payload changes must be backward compatible: add optional fields only. Anything
  else is a new version, and producers publish both until consumers migrate (FR-EVT-10).
- **The envelope** is versioned by CloudEvents `specversion` plus the AIOS schema id
  `aios://schemas/envelope/1`. New extension attributes are a minor, compatible change.
- **Manifests** follow Kubernetes API maturity: `v1alpha1` may change without notice;
  `v1beta1` keeps compatibility within the beta; `v1` is stable.
- Schemas are JSON Schema 2020-12. `$id`s use the `aios://schemas/` scheme; the schema registry
  service (FR-EVT-10, P1) will serve them over HTTP.
