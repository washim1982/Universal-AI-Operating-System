# ADR-019: Installation is desired state in the database, with consent that holds until it is changed

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-02
- **Deciders:** Tech lead
- **Requirements:** FR-APP-02 (schedules, webhooks), 03, 04, 05; FR-CP-01 (reconciler); FR-TOOL-03;
  M3 exit 4

## Context

A process used to run the Apps it was started with. That can't give a tenant admin a say in what
an App may do (FR-APP-03), can't change an App without restarting every process, and can't tell a
worker that started later what to run. M3 exit 4 asks that installing an App shows a consent step,
and that a permission refused there blocks that tool at runtime.

## Decision

**An installation is a record, and the record is what should be running.** One row per tenant and
App holds its status, the version that runs, a version waiting for consent, its configuration, what
was granted and what was refused. Every version a tenant has had is kept beside it (ADR-015).
`InstallationStore.save` changes the row and publishes what happened (`app.installed`,
`app.upgraded`, `app.version.restored`, `app.config.changed`, `app.consent.requested`,
`app.consent.refused`, `app.uninstalled`) in one transaction, so a process that hears of a change
always finds it.

**Every process reconciles.** Each API and worker process runs a `Reconciler`: it listens for those
events, reads every installation when it starts, and reads them all again every 30 seconds in case
it missed something. It changes only the Apps it installed itself, so Apps a server was started
with (`--app`) stay as they were. A bad version can't stop it running the good ones.

**Offering and agreeing are separate, and so are the people.** `app.install` (only `platform-admin`,
because a bundle's code is imported into a shared process) *offers* a version. The new
`app.manage` (`tenant-admin` and `platform-admin`) *agrees*, refuses, configures, rolls back and
uninstalls. A version is refused when offered if its agents can't be loaded, so a broken version
never becomes what every process tries to run, and the old one keeps going.

**Consent is what an App could do, not what it says it will.** The request lists the tools in the
App's `permissions`, its agents' allow lists and its workflows' tool steps; the memory scopes it
and its agents use; its data classes; and the hosts on its egress lists. It also says what can start
a run (chat, a schedule, a webhook), for information.

**What wasn't granted is refused at runtime.** A `consent` policy is installed with the App's own:
deny `tool.invoke` for every tool outside the grant, and deny memory reads and writes in refused
scopes. In Cedar a forbid beats any permit, so the App's own policies can't undo it. Data classes
are checked where a run starts (chat, and the engine for event entry points), and refused hosts
are taken off the egress lists.

**A "no" holds until someone says otherwise.** What was refused is recorded. Agreeing to a later
version leaves it out unless the person says they've changed their mind (explicit `grants`, or
`aios app consent --all`). A version that asks for nothing the tenant hasn't already answered runs
at once. A version never runs with more than it asked for, so a rollback gives up what only the
newer version had, and going forward again asks again.

**A version never changes.** The same version with another digest is a conflict. An upgrade keeps
the configuration unless it is given a new one; a configuration that makes an App ask for more
(`${config.*}` in a tool list) waits for consent like an upgrade.

**In-flight runs finish on their version's workflow.** Every version's workflows stay loaded, keyed
by name and version.

**Uninstall says what happens to data.** `retain` keeps the events and the App's memory; `delete`
erases the App's own memory (a person's own memories aren't the App's); `export`, in the CLI,
saves everything as JSON lines first and then deletes. Running runs are cancelled. Uninstalling
forgets the answers, so installing again asks again.

**Schedules and webhooks are entry points** (FR-APP-02). A schedule is a five-field cron in UTC.
Every worker keeps time, and one of them wins each minute's tick through a row in the database. A
missed minute isn't made up. A webhook is `POST /v1/hooks/{tenant}/{app}/{name}` with
`X-AIOS-Signature: t=<unix seconds>,v1=<HMAC-SHA256 of "<t>.<body>">`, the scheme approval
webhooks already use. The secret is one the App declares in `spec.config.secrets`. A request
older or newer than five minutes, a wrong signature, an App or hook that isn't there, and a missing
secret all get the same 401, so the route confirms nothing to someone who isn't signed in. Each
signature works once. A webhook's input is untrusted by default and taints its run.

**Which provider serves a tool is chosen per environment and per tenant** (FR-TOOL-03).
`AIOS_TOOL_BINDINGS=tool=provider,tenant/tool=provider`. A tenant's choice beats the
environment's, and a choice whose provider isn't there fails the call rather than falling back to
a provider someone decided against.

## Alternatives considered

- **Keep `--app` as the only way.** No consent and no change without restarts.
- **One reconciler, telling processes what to do.** One more service to run and to keep alive, and
  processes that can't start until it answers.
- **Consent as a flag on the App's own policy.** The App would be checking itself.
- **Narrow the agents' allow lists instead of a deny policy.** Workflow tool steps and memory have
  their own paths; one deny policy covers all of them in the engine that decides everything else.
- **Ask again on every upgrade.** Simple, but a person who is asked the same thing every week stops
  reading, and an upgrade could quietly undo a "no".
- **Leader election for schedules.** A row per tick does the same with nothing to elect.

## Consequences

- An admin's refusal survives upgrades. Asking for a broader pattern (`mail.>` where `mail.send`
  was refused) is a new ask, shown on the consent screen.
- A new entry point alone doesn't ask for consent. Its runs have only what was granted and the same
  budgets, but a person may want to know an App now runs on a schedule; it is listed under `starts`.
- **An agent's code isn't pinned per version.** Workflows are, but a run that was mid-task during
  an upgrade continues on the new code, because Python can hold one version of a module at a time.
  Agents in their own processes (ADR-014) fix this.
- Workers notice a change at once, and within 30 seconds if they missed the event.
- A minute's tick is lost if every worker is down for that minute.
- Tick and webhook records are kept for an hour, then let go.
- A webhook's dedupe is by signature: two identical requests in the same second count as one. A
  sender that means two should send two bodies.

## Implementation

`src/aios/core/apps.py`, `src/aios/core/schedule.py`; `src/aios/controlplane/installations.py`,
`postgres.py`, `installer.py`, `reconciler.py`, `runtime.py` (`fire_schedules`, `receive_webhook`,
`uninstall`); `src/aios/security/enforcement.py` (`consent_policy`); `src/aios/tools/service.py`
(`choose`); `src/aios/api/app.py` (`/v1/installations`, `/v1/hooks`).
`tests/unit/controlplane/test_installer.py`, `test_entrypoints.py`, `tests/unit/api/test_installations_api.py`,
`tests/unit/core/test_apps.py`, `test_schedule.py`, `tests/unit/tools/test_tool_choices.py`,
`tests/conformance/test_installation_store.py`.
