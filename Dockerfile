FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv venv --relocatable /app/.venv && \
    uv sync --frozen --no-dev --no-install-project

# Split into two `uv sync` runs so the (expensive, third-party-heavy)
# dependency layer above stays cached across source-only changes — the
# project itself can't be installed until its source exists, and that
# second sync is fast since every dependency is already resolved.
COPY src/ ./src/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm

# Matches this org's non-Django service convention (e.g.
# ol_infrastructure/applications/kubewatch_webhook_handler,
# ol_superset): a dedicated, unprivileged app user rather than root.
RUN useradd -m -u 1000 appuser

WORKDIR /app
COPY --from=build --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=build --chown=appuser:appuser /app/src /app/src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Set at build time to the git short SHA (see docker-uv-image-builds skill's
# image-tagging convention) — surfaces as service.version in OTel spans/traces
# and the Sentry release, via core/config.py's GIT_SHA validation_alias.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

USER appuser

EXPOSE 8000
# --no-access-log: uvicorn's own access log is unstructured text; the
# structured JSON access log middleware (core/observability/middleware.py)
# replaces it on every mounted app. No Docker HEALTHCHECK / init-system
# wrapper — this service is deployed on K8s, which owns health checking via
# the startup/readiness/liveness probes in k8s/deployment.yaml, and exec-form
# CMD (no shell) already gets SIGTERM directly as PID 1, matching how every
# other service in this org runs (see dockerfiles/ol-python-base and
# learn-ai/mit-learn's own Dockerfiles — no tini/dumb-init anywhere).
CMD ["uvicorn", "ol_analytics_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
