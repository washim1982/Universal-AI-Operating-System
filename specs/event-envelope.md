# Event envelope v0.1

Every event in AIOS is a [CloudEvents 1.0](https://github.com/cloudevents/spec) event with AIOS
extension attributes (ADR-002). Machine-readable schema:
[schemas/envelope.v1.json](schemas/envelope.v1.json). Python model: `aios.core.envelope.Event`.

## Attributes

| Attribute | Required | Rule |
|---|---|---|
| `specversion` | yes | `"1.0"` |
| `id` | yes | ULID (26 Crockford base32 characters, uppercase). Unique per event; sortable by time. |
| `source` | yes | URI-reference of the producer, e.g. `aios://acme/apps/ai-researcher/agents/research-agent`. No whitespace. |
| `type` | yes | The logical topic ([topic-grammar.md](topic-grammar.md)), e.g. `task.completed`. |
| `subject` | no | What the event is about within the source, e.g. `run/<runid>`. |
| `time` | yes in AIOS | RFC 3339 timestamp; always stored and emitted in UTC (`Z`). Naive times are rejected. |
| `datacontenttype` | no | Default `application/json`. |
| `dataschema` | no | `aios://schemas/<type>/<version>` for catalog events. |
| `tenantid` | yes | Tenant id (DNS label). **Set by the SDK/fabric from the caller's scope, never by agent code.** |
| `appid` | yes | App id (DNS label). `aios` is reserved for tenant-level platform events. |
| `runid` | no | ULID of the run. Also the default ordering key (FR-EVT-05). |
| `correlationid` | no | ULID grouping a run or a conversation. |
| `causationid` | no | Id of the event that directly caused this one. Builds the run DAG. |
| `hopcount` | yes | Causal depth, ≥ 0. Each derived event adds 1; the fabric rejects events above the limit (default 64) with `budget.exceeded` (FR-EVT-11). |
| `principal` | yes | Who acted: `<kind>:<name>[@<tenant>]`, kind ∈ `user`, `agent`, `app`, `service`, `system`. |
| `onbehalfof` | no | For whom (delegation, FR-SEC-05). Same format. |
| `dataclass` | yes | `public` < `internal` < `confidential` < `restricted`. Default `internal`. Derived events never get a lower label (FR-SEC-11). |
| `traceparent` | no | W3C Trace Context, version `00` (CloudEvents distributed-tracing extension). |
| `idempotencykey` | no | 1–255 characters; for commands that cause side effects (NFR-06). |
| `data` | no | Any JSON value. Catalog events have a payload schema. |

Unknown attributes are rejected in v0.1. Extension names are lowercase alphanumeric, as
CloudEvents requires.

## Deriving events

`Event.derive(type, data, source=…, principal=…)` creates the next event in a causal chain. The
child copies `tenantid`, `appid`, `runid`, `correlationid`, `onbehalfof`, `traceparent` and
`subject` (unless a new `subject` is passed); sets
`causationid` to the parent's id; increments `hopcount`; and takes the stricter `dataclass` of
the parent and the one requested. Classification is only ever raised by derivation; lowering it
is a governed policy action (Phase 2).

## Example

```json
{
  "specversion": "1.0",
  "id": "01JD7X2K9Q5V8M3N4P6R7S8T9V",
  "source": "aios://acme/apps/ai-researcher/agents/research-agent",
  "type": "task.completed",
  "subject": "run/01JD7X1C3BWQ8R2N5M4K6P7S9T",
  "time": "2026-10-28T10:15:30.123Z",
  "datacontenttype": "application/json",
  "dataschema": "aios://schemas/task.completed/1",
  "tenantid": "acme",
  "appid": "ai-researcher",
  "runid": "01JD7X1C3BWQ8R2N5M4K6P7S9T",
  "correlationid": "01JD7X1C3BWQ8R2N5M4K6P7S9T",
  "causationid": "01JD7X2A4H6J8K0M2N4P6Q8R0S",
  "hopcount": 4,
  "principal": "agent:research-agent@acme",
  "onbehalfof": "user:alice@acme",
  "dataclass": "internal",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "data": { "capability": "research.web", "output": { "summary": "…" } }
}
```
