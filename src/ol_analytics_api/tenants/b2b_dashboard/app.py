"""B2B dashboard tenant — org-manager and MIT-admin analytics for MIT Learn.

Mounted as an independent FastAPI sub-app (see main.py). Because it's a full
FastAPI() instance rather than a router included directly on the root app,
this tenant owns its own OpenAPI docs and auth model, all without touching
any other tenant.

Deliberately does NOT add its own request-logging middleware: Starlette's
Mount wraps the tenant's whole ASGI callable inside the root app's request
lifecycle, so the root app's middleware (main.py) already sees this
tenant's final response — status code, full duration — for every request
delegated here. Adding the same middleware here too would log every
request to this tenant twice.

It shares the root app's StarRocks connection pool (core/db/client.py,
started once in main.py's lifespan) but owns its own lifespan for
resources genuinely private to this tenant: the MITx Online httpx client,
and its own contribution to /health/readiness/.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.health import register_readiness_check
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import mitxonline_client
from ol_analytics_api.tenants.b2b_dashboard.routers import admin, organizations


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Fail fast and clearly if this app is ever served on its own (bypassing
    # main:app) instead of the confusing "RuntimeError from inside a query"
    # a request would otherwise hit — the StarRocks pool is shared/root-owned
    # and can't be started from here.
    if not starrocks_pool.is_started:
        msg = (
            "b2b_dashboard's StarRocks pool isn't started. This tenant must be "
            "served via ol_analytics_api.main:app, which owns the shared "
            "StarRocksPool lifecycle — not tenants.b2b_dashboard.app:app directly."
        )
        raise RuntimeError(msg)

    mitxonline_client.start()
    try:
        yield
    finally:
        await mitxonline_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="B2B Analytics Dashboard",
        description=(
            "Aggregated-only B2B site-license analytics for org managers and MIT "
            "contract admins. No individual learner PII."
        ),
        lifespan=lifespan,
    )
    app.include_router(organizations.router)
    app.include_router(admin.router)

    # Wires this tenant's live upstream dependency into the shared
    # /health/readiness/ — an unreachable MITx Online means requests behind
    # require_org_manager will fail, so the pod should be pulled from
    # rotation rather than silently reporting healthy.
    register_readiness_check(mitxonline_client.check_reachable)

    return app


app = create_app()
