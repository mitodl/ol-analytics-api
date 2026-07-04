FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv venv --relocatable /app/.venv && \
    uv sync --frozen --no-dev

COPY src/ ./src/

FROM python:3.12-slim-bookworm

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src

ENV PATH="/app/.venv/bin:${PATH}"

# Set at build time to the git short SHA (see docker-uv-image-builds skill's
# image-tagging convention) — surfaces as service.version in OTel spans/traces
# and the Sentry release, via core/config.py's GIT_SHA validation_alias.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

EXPOSE 8000
# --no-access-log: uvicorn's own access log is unstructured text; the
# structured JSON access log middleware (core/observability/middleware.py)
# replaces it on every mounted app.
CMD ["uvicorn", "ol_analytics_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
