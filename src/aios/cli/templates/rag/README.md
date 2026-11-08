# Aios Template App

An AIOS App that answers from its own documents, and says which ones it used.

## Run it

In one terminal, start AIOS with this App:

```bash
aios dev --app . --port 18080
```

In another, with `AIOS_SERVER=http://127.0.0.1:18080`:

```bash
aios memory ingest knowledge --app aios-template-app
aios run aios-template-app "How long do I have to return something?"
```

Put your own documents (Markdown or text) in `knowledge/` and ingest them again: only what changed is read again.

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
- `src/aios_template_app/agents.py`: the agent, which recalls from the App's memory.
- `knowledge/`: the documents.
- `tests/test_app.py`: a test that ingests them and asks a question.
