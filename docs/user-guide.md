# AIOS user guide

How to use AIOS, for each kind of person who works with it: the people who run the platform, the
people who run a tenant, the people who use and approve what its AI Apps do, the people who check
what happened, and the people who build Apps.

Find your role in [Who does what](#who-does-what), read [The basics](#the-basics) once, then go to
your section. To test AIOS step by step rather than use it, see the
[testing guide](testing-guide.md).

## Contents

- [The basics](#the-basics): words, ways in, signing in, setting up a terminal
- [Who does what](#who-does-what): the roles, and which section is yours
- [Platform administrator](#platform-administrator): runs the AIOS installation
- [Tenant administrator](#tenant-administrator): runs one organisation's space
- [Operator](#operator): keeps runs going
- [Member](#member): uses AI Apps
- [Approver](#approver): says yes or no to what an agent wants to do
- [Auditor](#auditor): checks what happened
- [App developer](#app-developer): builds AI Apps
- [Quick reference](#quick-reference): every command by role
- [When something goes wrong](#when-something-goes-wrong)

---

## The basics

### Words you will see

| Word | What it means |
|---|---|
| **Tenant** | One organisation's own space, such as `acme`. Its people, Apps, runs, memory and audit log are kept apart from every other tenant's |
| **App** | An AI application installed in a tenant, such as the AI Researcher or the AI Support Desk. It brings agents, workflows, tools and policies |
| **Agent** | A piece of an App that does one kind of work, using models and tools, for example a planner or a writer |
| **Workflow** | The steps of an App's job, in order: research, then analyse, then draft, then review |
| **Run** | One piece of work: a question answered, a ticket handled. It has an id such as `01M6FK1HE5X294BHWWWPQQRS97` |
| **Step** | One part of a run. A step can be done by an agent, call a tool, wait for a person, or wait for a signal |
| **Tool** | Something an agent can use: web search, a page fetch, a file, or an outside system such as a ticket desk |
| **Approval** | A request for a person to agree before an agent does something, such as a refund over $100 |
| **Memory** | What an App knows and remembers: documents it was given, facts it learned, and what it knows about a user |
| **Policy** | A rule about who may do what: which tools an agent may use, what needs an approval, what is never allowed |
| **Event** | A record of something that happened. Everything AIOS does is recorded as an event, and a run is its events |
| **Untrusted content** | Anything that came from outside, such as a web page, an email or a document. A run that has read some is marked `untrusted`, and from then on, anything risky needs a person |

### Three ways in

| Way in | Good for | How |
|---|---|---|
| **Console** | Most people. Runs and their traces, approvals, Apps, agents, costs, policies, memory, events | A web page. Ask your administrator for its address |
| **`aios` command** | Administrators, operators and developers. Everything the console does, and more | Comes with AIOS. Set it up once in each terminal: see [Setting up a terminal](#setting-up-a-terminal) |
| **API** | Other systems, and your own apps | HTTP with JSON, at `/v1/...`. Every request carries a credential |

The console has no permissions of its own: it calls the API as the person using it. So you can do
the same things in the console, with the command and through the API, and AIOS decides each time
whether your role allows it. If it doesn't, the console says so and gives the reason.

Commands in this guide start with `aios`, such as `aios get apps`, and work as written once you
have done [Setting up a terminal](#setting-up-a-terminal). Words in angle brackets stand for a value
of yours: type `aios status 01M6FK1HE5X294BHWWWPQQRS97 --app ai-researcher` for
`aios status <run-id> --app ai-researcher`, without the brackets.

### Signing in

| How | When |
|---|---|
| **API key** | People and services. A key looks like `aios_<id>_<secret>`, is shown once when made, and holds one or more roles in one tenant |
| **Identity provider** | People, when your organisation has set one up (Okta, Entra ID, Keycloak and others that speak OIDC). The console then has a **Sign in** button |
| **Nothing** | Only on your own machine, with `aios dev`. It has no sign-in and treats you as a platform administrator |

**In the console:** open its address. Either choose **Sign in** and use your organisation's
account, or paste your API key and choose **Sign in**. Your name and tenant show at the top right.
**Sign out** is next to them.

### Setting up a terminal

AIOS must already be running, in its own terminal or on a server. On your own machine, start it
first, as in [Install AIOS on one machine](#install-aios-on-one-machine), and leave that terminal
open. Then, in another terminal, in the AIOS folder, do these four steps. A terminal forgets them
when it closes.

**1. Make `aios` a command.** It is installed in the project's own Python environment, so a new
terminal doesn't know it yet. Turn the environment on:

```powershell
.\.venv\Scripts\Activate.ps1
```

**Expected:** the prompt starts with `(aios)`. If PowerShell says `running scripts is disabled on
this system`, use this instead, which needs no script:

```powershell
function aios { python -m uv run aios @args }
```

In bash, turn it on with `source .venv/bin/activate`, or `source .venv/Scripts/activate` in Git
Bash on Windows.

**2. Say where AIOS is.** Use the address your administrator gives you. On your own machine, it is
the port `aios dev` or `aios api` was started with. This guide starts them with `--port 18080`,
because other programs often hold the default, 8080: Llama AI Studio does.

```powershell
$env:AIOS_SERVER = "http://127.0.0.1:18080"
```

**3. Give your key,** unless your AIOS has no sign-in, as `aios dev` doesn't. Replace everything
between the quotes with your own AIOS key, which starts with `aios_`. Your tenant administrator
gives you one; see [Give people access](#give-people-access). It is not a model's key, such as
Llama AI Studio's or Anthropic's: those belong to the AIOS server, not to you (see
[Connect models](#connect-models-search-and-secrets)), and `aios` sends this key to `AIOS_SERVER`.

```powershell
$env:AIOS_API_KEY = "<your key>"
```

Against `aios dev`, skip this step. A key left over from another AIOS does no harm there; to
remove it, `Remove-Item Env:AIOS_API_KEY`.

In bash, steps 2 and 3 are `export AIOS_SERVER=http://127.0.0.1:18080` and
`export AIOS_API_KEY=<your key>`.

**4. Check it works:**

```powershell
aios get apps
```

**Expected:** the Apps installed in your tenant, each with its version, agents and workflows:

```text
[
  {
    "app": "ai-researcher",
    "version": "0.1.0",
    ...
```

| If you see | It means | Do |
|---|---|---|
| `The term 'aios' is not recognized` | Step 1 isn't done in this terminal | Do step 1 |
| `invalid choice: '...'` | `...` was typed as part of a command | Type the whole command, such as `aios get apps` |
| `did not answer like AIOS` | `AIOS_SERVER` points at another program, such as Llama AI Studio on 8080 | Fix step 2 |
| `no answer from AIOS at ...` | Nothing is listening at `AIOS_SERVER` | Start AIOS, or fix step 2 |
| `error 401` | The key is missing, wrong, revoked or expired | Fix step 3 |

---

## Who does what

AIOS has five built-in roles. Each role includes what the one above it can do, except `auditor`,
which can read but change nothing.

| Role | Can |
|---|---|
| `member` | See the Apps, start runs, read runs and their traces, replay and fork runs, and answer approvals a rule names them for |
| `auditor` | Read Apps, runs, traces, events, costs, everyone's approvals, policies and the audit log. Can't change anything or start runs |
| `operator` | What a member can, plus pause, resume, cancel and signal runs, retry them for real, read the event log and costs, and handle dead letters |
| `tenant-admin` | What an operator can, plus agree to Apps and configure, roll back and uninstall them, manage API keys, read the audit log and policies, manage memory, and use the kill switch |
| `platform-admin` | What a tenant admin can, plus offer new App code and read the platform's metrics |

An App can also name roles of its own in its policies, such as `support-lead` for refunds. Those
roles mean nothing elsewhere: they only say who an approval goes to.

### Your section

| You are... | Your role | Read |
|---|---|---|
| The person who installs and runs AIOS for everyone | `platform-admin` | [Platform administrator](#platform-administrator) |
| The person responsible for your organisation's Apps, keys and data | `tenant-admin` | [Tenant administrator](#tenant-administrator) |
| On call for the runs: keeping them moving, fixing what failed | `operator` | [Operator](#operator) |
| Someone who uses AI Apps to get work done | `member` | [Member](#member) |
| Someone who signs off on risky actions, such as refunds | `member` plus an App's role, such as `support-lead` | [Approver](#approver) |
| Compliance, security or audit | `auditor` | [Auditor](#auditor) |
| A developer building an AI App | `member` in a test tenant, and AIOS on your machine | [App developer](#app-developer) |

---

## Platform administrator

You install AIOS, keep it running, and look after everything shared between tenants: the
database, the models, the secrets, and the code that Apps bring.

### Install AIOS on one machine

You need Python 3.12 or later, [uv](https://docs.astral.sh/uv/), Docker, and Node.js 22 or later
for the console.

1. Get the code and install it:

   ```bash
   uv sync
   ```

2. Start AIOS with an App, in one process, with no sign-in. This is for your own machine only:

   ```bash
   aios dev --app apps/ai-researcher --cassettes tests/cassettes/ai-researcher --port 18080
   ```

   `--cassettes` answers model calls from a recording, so no model key is needed. Leave it out to
   use a real model. Leave the terminal open: AIOS runs until you press **Ctrl+C**.

3. In another terminal, do [Setting up a terminal](#setting-up-a-terminal) steps 1 and 2, then:

   ```bash
   aios run ai-researcher "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"
   ```

   **Expected:** the run's events as they happen, then a report and `status=completed`.

### Run AIOS for real: PostgreSQL, an API and workers

In production, AIOS is one API process and one or more worker processes, sharing PostgreSQL.

1. Start PostgreSQL. The compose file in `deploy/compose` starts it, with an OpenTelemetry
   Collector and Jaeger for traces. First copy `deploy/compose/.env.example` to
   `deploy/compose/.env` and set your own password there.

   ```bash
   docker compose -f deploy/compose/compose.yaml up -d
   ```

2. Start a worker. It runs the workflows and the agents. Start more for more capacity.

   ```bash
   aios worker --app apps/ai-researcher --database-url postgresql://aios:XXXXXX@127.0.0.1:5432/aios
   ```

3. Start the API:

   ```bash
   aios api --app apps/ai-researcher --database-url postgresql://aios:XXXXXX@127.0.0.1:5432/aios --port 18080
   ```

   The API requires a credential on every request. Add `--tls-cert` and `--tls-key` to serve
   HTTPS. `--tenant acme` installs the `--app` Apps for that tenant; repeat it for more tenants.

4. Start the console. Set `CONSOLE_SESSION_SECRET` to a long random value and
   `AIOS_CONSOLE_API` to the API's address as the console's container sees it, such as
   `http://host.docker.internal:18080`, then:

   ```bash
   docker compose -f deploy/compose/compose.yaml --profile console up -d
   ```

   People reach it at `http://127.0.0.1:3000`; `AIOS_CONSOLE_PORT` changes the port. To run it
   without Docker, see [console/README.md](../console/README.md).

### Create a tenant and its first administrator

A tenant comes into being with its first key. `aios platform` commands work on the database
directly, so give them its address first:

```powershell
$env:AIOS_DATABASE_URL = "postgresql://aios:XXXXXX@127.0.0.1:5432/aios"
```

```powershell
aios platform key --tenant acme --name acme-admin
```

**Expected:** one line, the key. It is shown only once. Give it to the tenant's administrator
through a safe channel. The key has the `tenant-admin` role; add `--role` to choose others, and
`--expires-in-days` to make it expire.

For yourself, make a `platform-admin` key in each tenant you look after:

```powershell
aios platform key --tenant acme --name platform --role platform-admin
```

### Let people sign in with your identity provider

1. Set `AIOS_OIDC_ISSUER` and `AIOS_OIDC_AUDIENCE` for the API. `AIOS_OIDC_ROLES_CLAIM` names the
   claim that holds a person's AIOS roles.
2. For the console, set `OIDC_ISSUER`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`. The console then
   shows **Sign in** instead of the key form.
3. To try it, `docker compose -f deploy/compose/compose.yaml --profile security up -d` starts
   Keycloak with a realm for AIOS, a client for the console, and test users.

API keys keep working beside sign-in, for services.

### Connect models, search and secrets

| What | Set |
|---|---|
| Claude | `ANTHROPIC_API_KEY` |
| A model on your own server (llama.cpp, vLLM, Ollama) | `AIOS_PRIVATE_MODEL`, `AIOS_PRIVATE_MODEL_URL`, and `AIOS_PRIVATE_MODEL_KEY` if it needs one. Every call in the default model policy then goes to it |
| Your own list of models | `AIOS_MODEL_REGISTRY`, the full path of a registry file |
| Web search | `AIOS_SEARCH_API_KEY`, and `AIOS_SEARCH_PROVIDER=keenable` for Keenable instead of Brave |
| Sites the page fetcher may open | `AIOS_HTTP_ALLOWED_DOMAINS`, unless an App lists its own |

Set these where the **worker** runs: it makes the model and tool calls.

A tool's credentials reach it only if its App lists them under `spec.config.secrets`. AIOS reads
them from Vault if `VAULT_ADDR` and `VAULT_TOKEN` are set, from an encrypted file if
`AIOS_SECRETS_FILE` and `AIOS_SECRETS_KEY` are set, and otherwise from `AIOS_SECRET_<NAME>`. To use
the encrypted file:

```bash
aios secrets new-key
```

Put the key it prints in `AIOS_SECRETS_KEY`, then store each secret. It asks for the value without
showing it:

```bash
aios secrets set --tenant acme --app ai-support-desk ZENDESK_TOKEN
```

### Offer an App to a tenant

Installing an App runs its code next to every tenant's, so only a platform administrator can
offer one. The tenant's administrator then decides what it may do.

1. Package the App. A package is reproducible: the same folder gives the same digest.

   ```bash
   aios app package apps/ai-support-desk
   ```

   **Expected:** it writes `ai-support-desk-<version>.json` and prints its digest.

2. Optionally, push it to your private registry, and install it from there later:

   ```bash
   aios app push apps/ai-support-desk registry.example.com/apps/ai-support-desk:0.2.0
   ```

3. With your `platform-admin` key for the tenant in `AIOS_API_KEY`, offer it:

   ```bash
   aios app install apps/ai-support-desk
   ```

   **Expected:** the App waits for consent. Tell the tenant's administrator; they agree in the
   console or with `aios app consent`. A version that can't load is refused here, and the one
   already running carries on.

Every process picks up the change within 30 seconds. Nothing needs a restart.

### Stop a whole tenant

The kill switch for a tenant stops its work at once: no new runs, no tasks, no model calls.

With `AIOS_DATABASE_URL` set as above:

```powershell
aios platform suspend --tenant acme --reason "suspected compromise"
```

```powershell
aios platform lift --tenant acme
```

These work on the database directly because a suspended tenant's keys stop working, including its
administrators'.

### Watch the platform

| What | Where |
|---|---|
| Is it up? | `GET /healthz`. Needs no credential |
| Metrics for Prometheus | `GET /metrics`, with a `platform-admin` key |
| Traces | Set `OTEL_EXPORTER_OTLP_ENDPOINT` for the worker, for example to Jaeger at `http://localhost:4318` |
| Logs | Each process writes JSON logs; `--log-level` sets how much |

---

## Tenant administrator

You decide which Apps your organisation uses and what they may do, who can sign in and with what
role, and what happens to your data. You can also stop any agent or App at once.

### Give people access

Make a key for each person or service, with the roles they need:

```bash
aios keys create alice --role member
```

```bash
aios keys create lead-sam --role member --role support-lead
```

**Expected:** the key, shown once. Hand it over safely. Keys you make can only be for your tenant,
and only a platform administrator can make a `platform-admin` key.

```bash
aios keys list
```

```bash
aios keys revoke <key-id>
```

A revoked key stops working at once. If your organisation uses an identity provider, people's
roles come from it instead, and keys are for services.

### Agree to an App

When a platform administrator offers an App or a new version, it waits for you.

**In the console:**

1. Open **Apps**, and the App marked `awaiting consent`.
2. Read what it asks for: the tools it may call, the memory it may use, the kinds of data it may
   handle, the sites it may reach, and what can start a run (chat, a schedule, a webhook).
3. Tick what you agree to. Anything you leave unticked is refused, and stays refused at runtime,
   even if the App's own policies would allow it. What you refused before starts unticked.
4. Choose **Agree to what is ticked**, or **Refuse version ...** to refuse this version.

**With the command:**

```bash
aios app show ai-support-desk
```

```bash
aios app consent ai-support-desk --deny-tool stripe.refund.create
```

`--deny-tool`, `--deny-scope`, `--deny-class` and `--deny-host` leave things out. `--all` agrees to
everything, including what you refused before. `aios app refuse ai-support-desk` refuses the
version.

A refusal holds across upgrades: a later version doesn't get it back unless you say so.

### Configure, upgrade and roll back

Each App can have settings, such as an approval threshold. In the console, open the App, change
**Configuration** (JSON) and choose **Save configuration**. With the command:

```bash
aios app configure ai-support-desk --config support-desk.json
```

AIOS checks the settings against the App's schema before they apply. A setting that would let the
App do more waits for your consent, like an upgrade.

To go back to an earlier version, find it under the App's **Versions** in the console and choose
**Go back to this**, or:

```bash
aios app rollback ai-support-desk 0.1.0
```

Runs already going finish on the version they started with.

### Remove an App

You choose what happens to its data:

| `--data` | What happens |
|---|---|
| `retain` | Its runs, events and memory are kept |
| `export` | Everything is saved to a file first, then deleted. Add `-o file.jsonl` |
| `delete` | The App's own memory is erased. What people remembered for themselves isn't the App's, and stays |

```bash
aios app uninstall notes --data export -o notes-export.jsonl
```

Its running runs are cancelled. Installing it again asks for consent again. On the console's App
page, **Export everything** downloads it all, and **Uninstall** asks what to do with its data.

### Stop an agent or an App at once

Use the kill switch when an agent misbehaves. It stops new work for that agent or App straight
away, including model calls already on their way.

**In the console:** open **Agents**, type a reason beside the agent and choose **Stop**. **Let it
work again** undoes it.

**With the command:**

```bash
aios suspend agent refund-agent --reason "refunding the wrong orders"
```

```bash
aios suspend app ai-support-desk --reason "investigating"
```

```bash
aios suspensions
```

```bash
aios unsuspend agent refund-agent
```

Every suspension and its reason is on the audit log.

### Look after memory

**Give an App documents to know:**

```bash
aios memory ingest docs/kb --app ai-support-desk
```

Run it again after the documents change: changed ones are updated, and ones that have gone are
removed (`--keep` keeps them). A URL works as well as a folder.

**Find and correct what is remembered:** in the console, open **Memory**, choose the scopes, type
what you are looking for and choose **Search**. Open an item, change its text and choose **Save
correction**, or choose **Forget this item**. Two people can't overwrite each other's corrections,
and every correction is on the audit log. Items under a legal hold can't be changed.

**Forget a person** when they ask, for example under GDPR:

```bash
aios memory forget --subject user:alice@acme
```

**Expected:** how many items were erased, and how many are held under a legal hold. Their memory
and its search entries are erased. See [What is kept](#what-is-kept-after-forget) for the limits.

### Check your tenant's record

```bash
aios audit verify
```

**Expected:** the audit chain verifies. If anyone changed or removed an entry, it says where.

```bash
aios audit list --limit 20
```

The console also shows **Costs** and budgets, **Policies** with a simulator, and **Events**: see
[Auditor](#auditor).

---

## Operator

You keep runs moving: you watch them, hold or stop the ones that go wrong, send them what they
are waiting for, and deal with work that failed for good.

### Watch runs

**In the console:** open **Runs**. Choose an **App** and a **Status**, type any text from the run,
tick to see only runs that read untrusted content, and choose **Search**. A run that is still
going refreshes by itself. Open a run to see its **Trace**: the
workflow's steps as a graph, and for each step its model calls, tool calls, policy decisions,
memory reads, approvals, tokens, cost and time, with a timeline below.

**With the command:**

```bash
aios status <run-id> --app ai-researcher
```

```bash
aios trace <run-id> --app ai-researcher
```

```bash
aios logs <run-id> --app ai-researcher
```

```bash
aios events tail --app ai-researcher
```

`status` shows the run's steps and what it waits for. `trace` shows what caused what. `logs` lists
the run's events. `events tail` follows everything that happens in an App.

### Pause, resume, cancel

```bash
aios pause <run-id> --app ai-researcher
```

```bash
aios resume <run-id> --app ai-researcher
```

```bash
aios cancel <run-id> --app ai-researcher
```

A pause takes effect between steps: work already asked for finishes, and nothing new starts. A
paused run survives restarts. The console's run page has **Pause**, **Resume** and **Cancel run**.
A run waiting for a person shows as paused too, but only a pause someone asked for can be resumed.

### Send a signal

Some workflows wait for something from outside, such as a payment confirmation. Send it:

```bash
aios signal <run-id> payment-received --app billing --data paid
```

`--data` takes JSON or plain text, and becomes the waiting step's output. A signal sent before the step starts waiting is kept
for it.

### Retry a failed run

A retry starts a finished run again from a step, for real: tools that change things are called
again, and people are asked again. Only operators and administrators can do it.

```bash
aios retry <run-id> --app ai-support-desk
```

It starts from the step that failed. Add `--from <step>` to start elsewhere. In the console, open
the run; under **Run again**, choose the step, tick **Do its side effects again, for real** and
choose **Retry for real**.

To try something without doing it for real, use a [fork](#replay-and-fork-a-run) instead.

### Deal with dead letters

Work that failed for good, after its retries, goes to the dead-letter list.

```bash
aios dlq list
```

```bash
aios dlq redrive <id>
```

```bash
aios dlq discard <id>
```

`redrive` sends it again, after you have fixed the cause. `discard` removes it. In the console,
open **Events**: under **Dead letters**, **Try again** and **Drop** do the same.

### Keep an eye on cost

Open **Costs** in the console: spending this month by App, agent, model and user, against each
budget, and earlier months by choosing a **Month**. Passing a soft limit sends `budget.warning`,
and the App's model policy can move it to a cheaper model. At a hard limit, runs stop cleanly with
`budget.exceeded`.

---

## Member

You use AI Apps to get work done, and you can see what they did and why.

### Ask an App

An App is started through its entry point: its own web page or chat, a schedule, a webhook, or the
API. With the command:

```bash
aios run ai-researcher "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"
```

**Expected:** the run's id on the first line, its events as they happen, then the answer and its
cost.

Through the API, from your own code. In PowerShell:

```powershell
Invoke-RestMethod "$env:AIOS_SERVER/v1/apps/ai-researcher/runs" -Method Post -Headers @{ Authorization = "Bearer $env:AIOS_API_KEY" } -ContentType "application/json" -Body '{"message": "What changed in 2026?"}'
```

In bash:

```bash
curl -X POST "$AIOS_SERVER/v1/apps/ai-researcher/runs" -H "Authorization: Bearer $AIOS_API_KEY" -H "Content-Type: application/json" -d '{"message": "What changed in 2026?"}'
```

**Expected:** `202` with the `run_id`. Read the run at `/v1/apps/ai-researcher/runs/<run-id>`, or
follow it live at `.../events`.

Some actions need a person's agreement first, such as a large refund. Your run then waits: it
shows `paused`, waiting for `approval:...`. It carries on when someone answers. Nobody acting for
you can approve it: whoever an action is for can't approve it.

### See what happened

**In the console:** open **Runs**, choose the App, and open your run. You see its status, its
cost, its output, and its **Trace**: every step with what it asked the models, which tools it
used, what policies decided, and how long each part took. Choose a step in the list to see its
details.

A yellow notice means the run read untrusted content, such as a web page, and lists where from.
From that point on, anything risky needed a person.

### Replay and fork a run

You can run a finished run again, to check or to try a change. Neither touches anything outside:
no tool that changes something is called, no message is sent, and nothing is remembered.

**Replay** runs it again with every model and tool call answered from the first run's record. It
costs nothing. If the App has changed since, so that the record can't answer, the replay stops
and says where (`replay.diverged`).

**Fork** starts it again from a step, with something changed: the run's input, that step's input,
or the model. Steps before it keep their results, and are marked `copied`.

**In the console:** open a finished run. Under **Run again**:

1. **Replay** runs it again. The new run says `A replay of <run>`, with **Compare the two**.
2. To fork, choose **From step**, change **New run input**, **New input for that step** or
   **Model**, and choose **Fork**. A new step input replaces the step's whole input: copy the
   step's **Input** from the trace and change it there.
3. **Compare the two** shows both runs step by step: status, cost, whether each output is the same,
   which steps were copied, and how many side effects were held back.

**With the command:**

```bash
aios replay <run-id> --app ai-researcher
```

```bash
aios fork <run-id> --app ai-researcher --from review --model anthropic/claude-opus-5-5
```

```bash
aios compare <run-id> <new-run-id> --app ai-researcher
```

`--input` and `--step-input` take JSON, or plain text. `compare` ends with `run output: same` or
`DIFFERENT`.

The **Runs** list marks replays and forks. To do something again for real, ask an operator for a
[retry](#retry-a-failed-run).

### What you can't do

Pausing, cancelling and retrying runs need an operator. Reading the event log, the audit log and
costs needs an operator or an auditor. If you try, the console or the command says which
permission is missing.

---

## Approver

You say yes or no when an agent wants to do something a policy says needs a person, such as a
refund over a limit. You need the role the App's rule names, for example `support-lead`, as well
as `member`.

### See what is waiting

**In the console:** open **Approvals**. It lists what is waiting for you, with its risk.

**With the command:**

```bash
aios approvals list
```

```bash
aios approvals show <approval-id>
```

### Decide

Open a request. Before you answer, read:

- **What would happen**: the exact action and its values, for example
  `pay.refund.create(amountUSD=120)`.
- **The risk**, and the policy that asked for you.
- **The agent's rationale**. It is the agent's claim, written by a model, and the console labels it
  so. If the run read untrusted content, the request says so: the rationale may have been written
  to persuade you.
- **Who it is for**, and when it expires.

Then choose **Approve** or **Deny**, with a comment if you like. The comment is recorded with your
decision.

```bash
aios approvals approve <approval-id> --comment "Checked the order: it arrived broken"
```

```bash
aios approvals deny <approval-id> --comment "Outside the return window"
```

### What to know

- **Silence means no.** If nobody answers before it expires, the action doesn't happen, unless the
  rule says otherwise.
- **You can't approve something for yourself.** If the run is acting for you, someone else has to
  answer it.
- **It happens once, or not at all.** An approved action runs once, even after a restart. The
  policies still apply when it runs: an approval can't get past a rule added meanwhile, or the
  kill switch.
- **First answer wins.** If someone else answered first, you are told it is already decided.

---

## Auditor

You check what happened, without changing anything: what each run did and why, who decided what,
what it cost, which rules applied, and that the record hasn't been tampered with.

### Check the audit log

```bash
aios audit verify
```

**Expected:** the chain verifies. Each entry is chained to the one before, so a changed or missing
entry shows.

```bash
aios audit list --limit 50
```

The audit log records every policy decision and tool call, approvals and their answers, keys made
and revoked, suspensions, suspected prompt injections, every change to an App (installs, consent,
configuration, upgrades, rollbacks, uninstalls), memory reads, writes, corrections and erasures,
documents ingested, and requests to pause, resume, cancel or signal a run.

### Read runs and their traces

Open **Runs** in the console, and any run's **Trace**: the policy decision for each tool call,
each approval and who answered it, what each step read and cost. Restricted data isn't shown in a
trace.

### Read everyone's approvals

In **Approvals**, under **Show**, choose **everyone's** with a status, such as `everyone's
pending` or `everyone's granted`, and choose **Show**.

```bash
aios approvals list --all --status granted
```

### Read the event log

Open **Events** in the console. Type a pattern such as `approval.>` or `memory.>` and choose
**Show**: newest first, with who did each thing. With the command:

```bash
aios events tail --app ai-support-desk --pattern "approval.>"
```

### See the rules, and ask what they would decide

Open **Policies** in the console and choose an App. You see its rules as written, the rules your
tenant administrator's consent added, and the policy as Cedar runs it.

The **simulator** asks the same engine that decides for real: choose an agent and an action, such
as calling `stripe.refund.create` with `{"amountUSD": 500}`, and whether the run has read
untrusted content, then choose **Ask**. It answers **Allowed**, **Refused** or **Needs a person**,
with the rule that decided. It changes nothing and leaves no trace.

### Check costs

Open **Costs**: spending this month and earlier months, by App, agent, model and user, with each
budget and how much of it is used.

---

## App developer

You build AI Apps: agents that use models and tools, workflows that put them in order, and
policies that say what they may do.

### Create an App

```bash
aios init my-app
```

**Expected:** a folder `my-app` with:

| File | What it holds |
|---|---|
| `manifests/app.yaml` | The App, its agent and its workflow |
| `src/my_app/agents.py` | The agent's code |
| `README.md` | How to run it |

### Run it on your machine

```bash
aios dev --app my-app --port 18080
```

In another terminal, do [Setting up a terminal](#setting-up-a-terminal) steps 1 and 2, then:

```bash
aios run my-app "Hello"
```

It needs a model: set `ANTHROPIC_API_KEY`, or point AIOS at a model on your own server with
`AIOS_PRIVATE_MODEL` and `AIOS_PRIVATE_MODEL_URL` (see
[Connect models](#connect-models-search-and-secrets)).

To make runs repeatable and free, record the model and tool calls once, then replay them:

```bash
aios dev --app my-app --cassettes cassettes/my-app --cassette-mode record --port 18080
```

```bash
aios dev --app my-app --cassettes cassettes/my-app --port 18080
```

### Write an agent

An agent is a Python function with a capability. It gets the task's input and a `ctx` that gives it
everything else:

```python
from pydantic import BaseModel

from aios import Context, agent


class Question(BaseModel):
    message: str


class Answer(BaseModel):
    answer: str


@agent("assistant-agent", capability="answering")
async def answer(ctx: Context, task: Question) -> Answer:
    hits = await ctx.tools.invoke("search.web.query", q=task.message, limit=5)
    return await ctx.model.generate(
        task="reasoning",
        system="Answer briefly, from the search results.",
        messages=[ctx.user(f"{task.message}\n\n{ctx.quote(str(hits), source='search')}")],
        output=Answer,
    )
```

| `ctx` | What it does |
|---|---|
| `ctx.model.generate(...)` | Calls a model, chosen by the App's model policy from a `task` hint, never by name. With `output=`, returns that model's type |
| `ctx.tools.invoke(name, ...)` | Calls a tool. Policy decides first; it may refuse, or wait for an approval |
| `ctx.memory.search / put / update / forget` | Reads and writes memory in the scopes the agent is allowed |
| `ctx.emit(type, data)` | Publishes a fact in the App's own domain, for other agents or Apps to react to |
| `ctx.step_key(name)` | An idempotency key for a side effect, the same on every attempt, so a retry can't do it twice |
| `ctx.quote(text, source=...)` | Marks outside text as quoted data, not instructions, in a prompt |

Everything an agent reads from outside is untrusted, and the run is marked so. Design for that: a
web page can contain instructions aimed at your agent. AIOS stops risky actions after that, but
your prompts should treat outside text as data.

### Put steps in order

A workflow in `manifests/app.yaml` lists its steps. Each step runs after the one before it, unless
it says what it `needs`.

| Step `type` | Does |
|---|---|
| `task` | Asks an agent with that capability. `input` binds what it gets, such as `${input.message}` or `${plan.output.tasks}` |
| `dynamic` | Fans out over a list a planner made, one task per item |
| `tool` | Calls a tool directly |
| `approval` | Waits for a person to agree, from `approvers` such as `role:support-lead` |
| `wait` | Waits for a signal from outside |

A step can loop back to an earlier one with `onResult`, up to `maxLoops`. Set `spec.budget` to
limit a run's cost, time and steps. The full reference is in
[specs/manifests.md](../specs/manifests.md).

### Say what your App may do

Policies are part of your App. For example, refunds over $100 need a support lead:

```yaml
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: refunds, version: 0.1.0}
spec:
  subject: {app: ai-support-desk}
  allow:
    - action: tool.invoke
      resource: stripe.refund.create
  requireApproval:
    - action: tool.invoke
      resource: stripe.refund.create
      when: {input: {amountUSD: {gt: 100}}}
      approvers: ["role:support-lead"]
```

Anything no rule allows is refused, and `deny` beats `allow`. List in `spec.permissions` the tools
and data classes your App needs: a tenant administrator agrees to them at install, and anything
they refuse is refused at runtime. `spec.egress` lists the sites your App's tools may reach.

### Test it

Write tests with `LocalRuntime`, which runs your App in memory, with cassettes for the model:

```python
from aios.testing.runtime import LocalRuntime


async def test_it_answers() -> None:
    async with LocalRuntime.from_app("my-app", cassettes="cassettes/my-app") as runtime:
        result = await (await runtime.chat("my-app", "Hello")).wait(timeout=30)
    assert result.status == "completed"
```

When a run goes wrong, [replay or fork it](#replay-and-fork-a-run): replay shows whether it is
repeatable, and a fork from the step that went wrong tries your change without doing anything for
real. `aios compare` shows what changed.

### Ship it

```bash
aios app package my-app
```

```bash
aios app push my-app registry.example.com/apps/my-app:0.1.0
```

Give the reference to your platform administrator, who offers it to tenants. Each version is
fixed: to change it, raise `metadata.version`.

---

## Quick reference

| I want to... | Console | Command | Role |
|---|---|---|---|
| See the Apps | **Apps** | `aios get apps` | member |
| Ask an App | the App's own page | `aios run <app> "<message>"` | member |
| See a run and its trace | **Runs** → the run | `aios status`, `trace`, `logs` | member |
| Replay or fork a run | run page → **Run again** | `aios replay`, `aios fork --from <step>` | member |
| Compare two runs | **Compare the two** | `aios compare <a> <b> --app <app>` | member |
| Answer an approval | **Approvals** | `aios approvals approve|deny <id>` | member, named by the rule |
| Pause, resume, cancel | run page | `aios pause|resume|cancel <run> --app <app>` | operator |
| Send a signal | | `aios signal <run> <name> --app <app>` | operator |
| Retry for real | run page → **Retry for real** | `aios retry <run> --app <app>` | operator |
| Handle dead letters | **Events** → **Dead letters** | `aios dlq list|redrive|discard` | operator |
| See costs | **Costs** | | operator, auditor |
| Follow the event log | **Events** | `aios events tail` | operator, auditor |
| See the rules, simulate | **Policies** | | auditor, tenant-admin |
| Check the audit log | | `aios audit verify`, `aios audit list` | auditor, tenant-admin |
| Everyone's approvals | **Approvals** → everyone's | `aios approvals list --all` | auditor |
| Make or revoke keys | | `aios keys create|list|revoke` | tenant-admin |
| Agree to an App | **Apps** → the App | `aios app consent <app>` | tenant-admin |
| Configure, roll back | **Apps** → the App | `aios app configure`, `aios app rollback` | tenant-admin |
| Uninstall an App | **Apps** → the App | `aios app uninstall <app> --data retain` (or `export`, `delete`) | tenant-admin |
| Stop an agent or App | **Agents** → **Stop** | `aios suspend agent <name> --reason "<why>"` (or `app`) | tenant-admin |
| Correct memory | **Memory** | | tenant-admin |
| Forget a person | | `aios memory forget --subject <principal>` | tenant-admin |
| Give an App documents | | `aios memory ingest <folder> --app <app>` | tenant-admin |
| Offer App code | | `aios app install <folder or reference>` | platform-admin |
| Create a tenant's first key | | `aios platform key --tenant <t> --name <n>` | database access |
| Stop a whole tenant | | `aios platform suspend --tenant <t> --reason "<why>"` | database access |
| Read metrics | | `GET /metrics` | platform-admin |

---

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `The term 'aios' is not recognized` | The terminal doesn't know the command yet | [Setting up a terminal](#setting-up-a-terminal), step 1 |
| `did not answer like AIOS` | `AIOS_SERVER` points at another program | [Setting up a terminal](#setting-up-a-terminal), step 2 |
| `no answer from AIOS at ...` | AIOS isn't running at `AIOS_SERVER` | Start it, or fix `AIOS_SERVER` |
| `error 401: sign in` | No key was sent, or it is wrong, revoked or expired | Set `AIOS_API_KEY`, or sign in again |
| `error 403` with a reason | Your role doesn't allow that | Ask your tenant administrator for the role |
| `tenant ... is suspended` | The kill switch is on for your tenant | Ask your platform administrator |
| A run shows `paused`, waiting for `approval:...` | It needs a person | See [Approver](#approver), or ask who can approve |
| A run waits forever | No worker is running, or it waits for a signal | Ask your operator |
| `budget.exceeded` | A cost limit was reached | Ask your tenant administrator to raise it, or wait for the month |
| `replay.diverged` | The App, its prompt or what it remembered changed since the run | Fork from that step instead |
| `error 409: ... only a finished run has all its record` | Replay, fork and retry need a finished run | Wait for it, or cancel it |
| `nothing failed in that run` | A retry starts where the run failed | Add `--from <step>` |
| The console says `That didn't load` | AIOS isn't reachable from the console | Ask your platform administrator |

### What is kept after forget

`aios memory forget` erases a person's memory items and their search entries. It doesn't yet
erase what is in the event log: the arguments of tool calls and approvals, and text a model wrote
from an erased item. Log retention and erasure are planned. Backups keep what they hold until they
expire.

For more about how AIOS keeps tenants apart and what each control stops, see the
[security review](security-review.md) and the [threat model](threat-model.md).
