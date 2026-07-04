"""B2B dashboard tenant — org-manager and MIT-admin analytics for MIT Learn.

Mounted as an independent FastAPI sub-app (see main.py). Because it's a full
FastAPI() instance rather than a router included directly on the root app,
this tenant owns its own OpenAPI docs, auth model, and — if a future
requirement needs it — its own middleware or exception handlers, all without
touching any other tenant. It shares only the root app's StarRocks
connection pool (core/db/client.py), started once in main.py's lifespan.
"""

from __future__ import annotations

from fastapi import FastAPI

from ol_analytics_api.core.observability.middleware import add_request_logging
from ol_analytics_api.tenants.b2b_dashboard.routers import admin, organizations


def create_app() -> FastAPI:
    app = FastAPI(
        title="B2B Analytics Dashboard",
        description=(
            "Aggregated-only B2B site-license analytics for org managers and MIT "
            "contract admins. No individual learner PII."
        ),
    )
    add_request_logging(app)
    app.include_router(organizations.router)
    app.include_router(admin.router)
    return app


app = create_app()
