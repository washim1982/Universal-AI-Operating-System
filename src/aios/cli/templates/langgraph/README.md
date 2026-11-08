# Aios Template App

An AIOS App whose agent is a LangGraph graph. Its model calls go through AIOS, so the model policy, budgets and traces apply. Needs `pip install "aios[langgraph]"`.

## Run it

In one terminal, start AIOS with this App:

```bash
aios dev --app . --port 18080
```

In another, with `AIOS_SERVER=http://127.0.0.1:18080`:

```bash
aios run aios-template-app "Hello"
```

To give the graph a tool, name it in the agent's `tools.allow` (for example `search.web.query`) and allow it in a policy: `build` then gets it, and calls it through AIOS.

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

- `manifests/app.yaml`: the App, its agent (`adapter:langgraph`) and its workflow.
- `src/aios_template_app/graph.py`: the graph.
- `tests/test_app.py`: a test that runs it.
