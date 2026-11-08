# Aios Template App

An AIOS App with one agent that answers a chat message with a model.

## Run it

In one terminal, start AIOS with this App:

```bash
aios dev --app . --port 18080
```

In another, with `AIOS_SERVER=http://127.0.0.1:18080`:

```bash
aios run aios-template-app "Hello"
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

- `manifests/app.yaml`: the App, its agent and its workflow.
- `src/aios_template_app/agents.py`: the agent.
- `tests/test_app.py`: a test that runs it.
