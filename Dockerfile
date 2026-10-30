# syntax=docker/dockerfile:1
# AIOS runtime image: one image, run as API or worker (ADR-001).

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so source changes don't invalidate this layer.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
LABEL org.opencontainers.image.title="AIOS Core" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN useradd --create-home --uid 10001 aios
COPY LICENSE NOTICE /usr/share/doc/aios/
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER aios
WORKDIR /home/aios
ENTRYPOINT ["aios"]
CMD ["--help"]
