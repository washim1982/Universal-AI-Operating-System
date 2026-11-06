# ADR-011: Identity — OIDC for people, API keys for services, named workloads for agents

- **Status:** Proposed — awaiting the project owner's review. WP 2.2 implements it behind ports, so
  changing it later does not touch the enforcement points.
- **Date:** 2026-11-01
- **Deciders:** Project owner, tech lead, security engineer
- **Requirements:** FR-SEC-01, 02, 04, 05, 13; principle P4

## Context

Until now the API took the tenant from an `X-AIOS-Tenant` header, which is safe only on a
developer's own machine. A test confirmed what that means: any caller can claim any tenant, and
the value `*` reads every tenant's events, because `Scope` treats it as "all tenants" for platform
components. Phase 2 puts two tenants on one installation, so who is calling, and for which tenant,
must come from a credential the caller can't forge.

Three kinds of principal need identities. People sign in through their organization. Services
and scripts need something that works without a browser. Agents run inside the worker and must
never hold a credential that a prompt could extract.

## Decision

**The tenant comes from the identity, never from the request.** An authenticated request that also
sends `X-AIOS-Tenant` is refused if the two differ.

**People: OpenID Connect.** AIOS validates bearer JWTs from one configured issuer: signature against
the issuer's published keys (RS256 or ES256 only), `iss`, `aud`, `exp` and `sub` all required.
Keycloak runs locally; any OIDC provider works. Two claims are configurable, the tenant
(`aios_tenant` by default) and the roles (`roles`; dotted paths such as `realm_access.roles` work).
The principal is `user:<sub>@<tenant>`. An issuer must use `https`, except on a loopback address.

**Services: API keys.** `aios_<id>_<secret>`, with a 256-bit random secret. Only its SHA-256 digest
is stored: a slow hash protects guessable passwords, and these can't be guessed. A key belongs to
one tenant and has a name, roles and an optional expiry. Its principal is
`service:<name>@<tenant>`. It is shown once, when created, and can be revoked.

**Agents: named workloads, no credential.** An agent's identity is its envelope principal
`agent:<name>@<tenant>`, which the kernel stamps, and which maps to the SPIFFE-compatible id
`spiffe://<trust-domain>/tenant/<tenant>/app/<app>/agent/<name>`. In Phase 2 agents share the worker
process, so the kernel asserts the identity and agent code never sees a token. mTLS and issued
SVIDs arrive with process isolation (FR-SEC-02 P1, ADR-014).

**Roles.** Five built-in roles, defined as ordinary Cedar policies in
`src/aios/security/defaults/roles.yaml`:

| Role | Can |
|---|---|
| `member` | List Apps, start and read runs, have agents use tools on their behalf, and answer approvals a rule names them for |
| `auditor` | Read Apps, runs, events, approvals and the audit log |
| `operator` | `member`, plus run control, the event log and dead letters |
| `tenant-admin` | `operator`, plus the audit log, API keys and the kill switch |
| `platform-admin` | `tenant-admin`, plus installing Apps and reading `/metrics` |

Any other role name grants nothing until a policy mentions it, which is how `support-lead` works.
An approver holds that role and a built-in one, because custom roles reach no control-plane action.

`platform-admin` is separate because a manifest names a Python entrypoint and tenants share a
process, so installing an App is running code next to every other tenant. The first one needs
direct database access (`aios platform key`), and only a `platform-admin` can grant it through the
API. A `tenant-admin` can grant every other role, including a tenant's own such as `support-lead`.
This restriction can relax once agents are sandboxed (ADR-014).

**Delegation (FR-SEC-05).** A run keeps its caller in the envelope's `onbehalfof`. Each tool call
is decided for the agent and for that caller, and both must allow it. The caller's roles are read
from the directory at decision time, not copied onto events, so taking a role away also applies to
a run that is already paused. Anonymous runs on a local server carry no delegate, and the agent's
own permissions apply.

**Local development.** `aios dev` and `aios api --no-auth` take no credentials and treat the caller
as `platform-admin` of the tenant in the header, which lets a developer work without an identity
provider. Both refuse a non-loopback address, so this mode can't be exposed by accident.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep the header behind a gateway | Moves the boundary outside AIOS. One misconfigured route exposes every tenant, and the isolation suite couldn't test it. |
| Long-lived tokens for agents | A credential in the agent's process can be extracted by a prompt or a dependency. Agents don't need one to be identified. |
| Session cookies issued by AIOS | Makes AIOS an identity provider, with password storage, reset flows and MFA. OIDC gets all of that from the customer's own provider. |
| Roles copied onto every event | Simple, and it records what was true at the time, but a revoked role would keep working for any run already under way. |
| bcrypt or argon2 for API keys | Costs tens of milliseconds on every request and protects nothing here, because the secret has 256 bits of entropy. |

## Consequences

- `aios api` refuses to start without a way to sign in, unless it is told `--no-auth` on a loopback
  address. Existing scripts must add a key, or that flag.
- Every enforcement point can name a principal, so the audit log says who did what.
- AIOS depends on PyJWT, and on the operator running an OIDC provider for human sign-in.
- Members can read every run in their tenant. Per-run ownership is not part of this decision.
- `tenants` are implicit: a tenant exists once something refers to it. The Organization and
  Environment levels of FR-SEC-01 remain to be built.

## Implementation

`src/aios/core/identity.py` (ports), `src/aios/security/identity.py` (API keys, OIDC),
`src/aios/security/enforcement.py` (delegation, kill switch), `src/aios/api/app.py` (the boundary),
`tests/unit/security/test_identity.py`, `tests/unit/api/test_authorization.py`,
`tests/isolation/`.
