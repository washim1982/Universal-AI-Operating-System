# Aios Template App

An AIOS App that refunds orders with its own tool. A refund over $100 waits for a person with the `approver` role, who sees what the tool says it would do.

## Run it

In one terminal, start AIOS with this App:

```bash
aios dev --app . --port 18080
```

In another, with `AIOS_SERVER=http://127.0.0.1:18080`:

```bash
aios run aios-template-app "Refund order 1001 $250"
aios approvals list
aios approvals approve <approval-id>
```

Start `aios dev` with the App's tool:

```bash
AIOS_TOOL_PROVIDERS=aios_template_app.tools:providers aios dev --app . --port 18080
```

(In PowerShell, set `$env:AIOS_TOOL_PROVIDERS` first.)

## Test it

```bash
pytest
```

The tests use a stand-in model, `aios.testing.models.ScriptedModel`: no key, no cost. Change the
replies it is given to try other answers.

## Install it

```bash
aios app install .
```

A tenant administrator then agrees to what it asks for (`aios app consent aios-template-app`).

## What's here

- `manifests/app.yaml`: the App, its agent and its workflow.
- `policies/refunds.yaml`: who must agree, and when.
- `src/aios_template_app/agents.py`: the agent.
- `src/aios_template_app/tools.py`: the tool, with its dry run for approvers.
- `tests/test_app.py`: a small refund, and a big one that waits.
