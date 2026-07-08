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

Also deliberately does NOT pass its own `lifespan=` to FastAPI(): a mounted
sub-app's lifespan is never invoked by the ASGI protocol at all — only the
top-level app uvicorn is pointed at receives lifespan.startup/shutdown
messages (confirmed by reading Starlette's Router.lifespan(), which only
ever runs its own app's lifespan_context, with no propagation to routes).
Startup/shutdown for this tenant's own resources (the MITx Online client)
is instead exposed as on_startup()/on_shutdown() below, which main.py's
root lifespan calls explicitly via the TENANTS registry — see main.py.
"""

from __future__ import annotations

from fastapi import FastAPI

from ol_analytics_api.core.errors import add_shared_error_handlers
from ol_analytics_api.core.health import register_readiness_check
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import mitxonline_client
from ol_analytics_api.tenants.b2b_dashboard.routers import admin, organizations

# Names this tenant's readiness sub-path (/health/readiness/b2b_dashboard/).
TENANT_NAME = "b2b_dashboard"


async def on_startup() -> None:
    mitxonline_client.start()


async def on_shutdown() -> None:
    await mitxonline_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="B2B Analytics Dashboard",
        description=(
            "Aggregated-only B2B site-license analytics for org managers and MIT "
            "contract admins. No individual learner PII."
        ),
    )
    app.include_router(organizations.router)
    app.include_router(admin.router)

    # Turn a saturated shared StarRocks pool into a fast 503 for this tenant's
    # requests rather than a 500 (a mounted sub-app handles its own
    # exceptions — a handler on the root app never sees these).
    add_shared_error_handlers(app)

    # Exposes this tenant's live upstream dependency at its OWN readiness
    # sub-path (/health/readiness/b2b_dashboard/), for monitoring and
    # per-tenant routing. Scoped to this tenant on purpose: an unreachable
    # MITx Online fails only requests behind require_org_manager here, so it
    # must not be able to fail the shared /health/readiness/ probe and pull
    # the whole pod — that would take down every other tenant mounted on it.
    register_readiness_check(TENANT_NAME, mitxonline_client.check_reachable)

    return app


app = create_app()
