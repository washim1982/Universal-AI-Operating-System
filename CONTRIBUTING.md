# Contributing to AIOS Core

Thank you for helping. AIOS Core is licensed under the [Apache License 2.0](LICENSE); by
contributing you agree that your contribution is licensed the same way (ADR-012).

## Sign your commits (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a
contributor license agreement. Every commit in a pull request must carry a sign-off line that
matches the commit author:

```
Signed-off-by: Ada Lovelace <ada@example.com>
```

`git commit -s` adds it for you. The sign-off certifies that you wrote the change, or have the
right to submit it, under the project's license. CI checks every commit of a pull request
(`scripts/check-dco.sh`); to fix a missing sign-off, run `git rebase --signoff main` and
force-push your branch.

## Development setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                      # .venv with runtime and dev dependencies
uv run pre-commit install    # git hooks: ruff, layering contract, generated specs
uv run pytest                # unit, conformance and acceptance tests
```

Before opening a pull request, run the same checks as CI:

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run aios specs check
uv run pytest --cov --cov-fail-under=80
```

## What goes where

- **Design changes** start as an ADR in [docs/adr/](docs/adr/) (copy the template). Accepted
  ADRs are never rewritten; a later change supersedes them with a new ADR.
- **Layering** (ADR-001): `core` ← `fabric` ← runtime components ← access layer. Runtime
  components never import each other. `uv run lint-imports` enforces this.
- **Specs** under `specs/schemas/` are generated from the Pydantic models. Change the models,
  then run `uv run aios specs export` and commit the result.
- **New event types** go into the catalog (`src/aios/core/catalog.py`) with a payload model.
- **Dependencies** must use a license compatible with Apache-2.0 (permissive licenses such as
  MIT, BSD, Apache-2.0, ISC or PSF). `tests/unit/test_licenses.py` checks every runtime
  dependency; anything else needs an ADR.
- **Core versus Enterprise:** see [docs/editions.md](docs/editions.md). Features needed to run
  a single-tenant production deployment safely always belong in Core.

## Reporting security issues

Please do not open a public issue for a vulnerability. Until a security contact is published,
report it privately to the maintainers.
