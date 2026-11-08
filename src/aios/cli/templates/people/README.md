# Aios Template App

An AIOS App where a person reviews each request before an agent replies. The review is work people do as an agent would: it waits under **Tasks** in the console.

## Run it

In one terminal, start AIOS with this App:

```bash
aios dev --app . --port 18080
```

In another, with `AIOS_SERVER=http://127.0.0.1:18080`:

```bash
aios run aios-template-app "Can I change my delivery address?"
aios tasks list
aios tasks complete <task-id> --field accept=true --field note=fine
```

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

- `manifests/app.yaml`: the App, the people who review (`spec.humans`), the agent and the workflow.
- `src/aios_template_app/agents.py`: the agent that replies.
- `tests/test_app.py`: a review, then the reply.
