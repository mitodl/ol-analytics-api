"""Root ASGI app: shared infra (StarRocks pool, observability) + mounted
tenant sub-apps.

Each tenant is a fully independent `FastAPI()` instance — its own routers,
auth/governance model, OpenAPI docs — mounted at its own path prefix. This
is what lets ol-analytics-api serve more than one audience: the first
consumer is the b2b_dashboard tenant (org managers + MIT contract admins),
but a future consumer (a different internal tool, a partner integration,
a public read-only feed) is added by writing a new package under
`tenants/` with its own `app.py`, and adding one line to TENANTS below —
without touching b2b_dashboard's code, auth, or URL surface at all.

What's shared across every tenant lives in `core/`: the StarRocks
connection pool (core/db/client.py, started once here and imported by
each tenant's routers), the generic X-Userinfo decode
(core/auth/userinfo.py), and observability (core/observability/,
core/health.py) — structured logging, OpenTelemetry tracing, Sentry, and
the tiered K8s health checks every service in this org exposes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ol_analytics_api.core import health
from ol_analytics_api.core.config import settings
from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.db.vault_credentials import fetch_starrocks_credentials
from ol_analytics_api.core.observability.logging import configure_structlog
from ol_analytics_api.core.observability.middleware import add_request_logging
from ol_analytics_api.core.observability.sentry import init_sentry
from ol_analytics_api.core.observability.telemetry import configure_opentelemetry

# Sentry first, so it can capture errors in the setup that follows.
init_sentry(
    dsn=settings.sentry_dsn,
    environment=settings.environment,
    version=settings.service_version,
    log_level=settings.sentry_log_level,
    traces_sample_rate=settings.sentry_traces_sample_rate,
    profiles_sample_rate=settings.sentry_profiles_sample_rate,
)
configure_structlog(debug=settings.debug, log_level=settings.log_level)
configure_opentelemetry(
    service_name=settings.service_name,
    service_version=settings.service_version,
    environment=settings.environment,
    debug=settings.debug,
)

# Tenant sub-apps are imported only after configure_opentelemetry() runs:
# FastAPI auto-instrumentation patches FastAPI.__init__, so a FastAPI()
# instance constructed earlier — including each tenant's module-level
# `app = create_app()` — would be silently uninstrumented.
from ol_analytics_api.tenants.b2b_dashboard.app import app as b2b_dashboard_app  # noqa: E402

# (mount_path, sub_app) — add a new tenant by appending here.
TENANTS: list[tuple[str, FastAPI]] = [
    ("/api/v1/analytics", b2b_dashboard_app),
]


def _resolve_starrocks_credentials() -> tuple[str, str]:
    if settings.vault_addr:
        return fetch_starrocks_credentials()
    # Local dev fallback — matches bin/starrocks-auth --output env's
    # STARROCKS_USER / STARROCKS_PASSWORD, no OL_ANALYTICS_API_ prefix.
    return os.environ["STARROCKS_USER"], os.environ["STARROCKS_PASSWORD"]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    user, password = _resolve_starrocks_credentials()
    await starrocks_pool.start(user, password)
    try:
        yield
    finally:
        await starrocks_pool.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="OL Analytics API",
        description="Multi-tenant read-only analytics gateway over StarRocks.",
        lifespan=lifespan,
    )

    add_request_logging(app)
    app.include_router(health.router)

    for mount_path, tenant_app in TENANTS:
        app.mount(mount_path, tenant_app)

    return app


app = create_app()
