# AI Researcher cassette

`cassette.json` holds the model and search responses of one AI Researcher run, keyed by call
site (`<step>[#item]/<agent>/l<loop>a<attempt>/<model|tool>/<n>`, see `aios.core.cassettes`),
so the acceptance tests run offline and deterministically.

**This recording is synthetic.** It was made through the real runtime, in record mode, with a
stand-in model provider returning plausible structured outputs and a stand-in search tool, so
every call-site key and token count is what the runtime produces, but the text was not written
by a model. Replace it with a live recording when API keys are available:

```bash
AIOS_CASSETTE_MODE=record ANTHROPIC_API_KEY=... AIOS_SEARCH_API_KEY=... \
  uv run pytest tests/e2e/test_ai_researcher.py -k cited_sources
```

Record mode calls the real providers and overwrites the interactions it makes. Recordings
that differ only in content keep replaying (the request hash is checked as a warning).
