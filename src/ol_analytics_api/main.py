"""Root ASGI app: shared infra (StarRocks pool, observability) + mounted
tenant sub-apps.

Each tenant is a fully independent `FastAPI()` instance — its own routers,
auth/governance model, OpenAPI docs — mounted at its own path prefix. This
is what lets ol-analytics-api serve more than one audience: the first
consumer is the b2b_dashboard tenant (org managers + MIT contract admins),
but a future consumer (a different internal tool, a partner integration,
a public read-only feed) is added by writing a new package under
`tenants/` with its own `app.py`, and adding one entry to TENANTS below —
without touching b2b_dashboard's code, auth, or URL surface at all.

What's shared across every tenant lives in `core/`: the StarRocks
connection pool (core/db/client.py, started once here and imported by
each tenant's routers), the generic X-Userinfo decode
(core/auth/userinfo.py), and observability (core/observability/,
core/health.py) — structured logging, OpenTelemetry tracing, Sentry, and
the tiered K8s health checks every service in this org exposes.

A mounted sub-app's own `lifespan=` is never invoked by the ASGI protocol —
only the top-level app uvicorn points at receives lifespan.startup/shutdown
messages (Starlette's Router.lifespan() only ever runs its own
lifespan_context, with no propagation into routes/Mounts). So a tenant's
startup/shutdown hooks (e.g. b2b_dashboard's MITx Online client) are
plain async functions the root lifespan calls explicitly, once per tenant,
via the TENANTS registry below — not something each tenant's own app.py can
wire up on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
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

log = structlog.get_logger(__name__)

# Tenant sub-apps are imported only after configure_opentelemetry() runs:
# FastAPI auto-instrumentation patches FastAPI.__init__, so a FastAPI()
# instance constructed earlier — including each tenant's module-level
# `app = create_app()` — would be silently uninstrumented.
from ol_analytics_api.tenants.b2b_dashboard import app as b2b_dashboard  # noqa: E402


@dataclass(frozen=True)
class Tenant:
    mount_path: str
    app: FastAPI
    on_startup: Callable[[], Awaitable[None]] | None = None
    on_shutdown: Callable[[], Awaitable[None]] | None = None


# Add a new tenant by appending a Tenant() entry here.
TENANTS: list[Tenant] = [
    Tenant(
        "/api/v1/analytics",
        b2b_dashboard.app,
        on_startup=b2b_dashboard.on_startup,
        on_shutdown=b2b_dashboard.on_shutdown,
    ),
]


# Vault-issued StarRocks credentials are a dynamic user with a lease — once
# it expires, Vault revokes that user and new connection attempts with
# those credentials start failing authentication. The refresh loop below
# re-fetches (a new lease, a new dynamic user) well before that happens and
# rotates the pool over, rather than waiting for expiry-driven auth
# failures. Refreshing at 80% of the lease lifetime leaves headroom for a
# slow Vault round-trip or a retry; the floor keeps a very short lease
# (e.g. in a test environment) from turning into a busy-loop.
_CREDENTIAL_REFRESH_SAFETY_MARGIN = 0.8
_CREDENTIAL_REFRESH_MIN_INTERVAL_SECONDS = 60.0
_CREDENTIAL_REFRESH_RETRY_SECONDS = 60.0


async def _resolve_starrocks_credentials() -> tuple[str, str, int | None]:
    """Returns (user, password, lease_duration_seconds). lease_duration is
    None for the local-dev static-credential fallback, which never expires
    and therefore never needs the refresh loop below."""
    if settings.vault_addr:
        # fetch_starrocks_credentials() is fully synchronous (file read +
        # hvac/requests HTTP calls) — offload it so a slow Vault doesn't
        # block the event loop during startup.
        return await asyncio.to_thread(fetch_starrocks_credentials)
    # Local dev fallback — matches bin/starrocks-auth --output env's
    # STARROCKS_USER / STARROCKS_PASSWORD, no OL_ANALYTICS_API_ prefix.
    return os.environ["STARROCKS_USER"], os.environ["STARROCKS_PASSWORD"], None


def _next_refresh_delay(lease_duration: int) -> float:
    return max(
        lease_duration * _CREDENTIAL_REFRESH_SAFETY_MARGIN,
        _CREDENTIAL_REFRESH_MIN_INTERVAL_SECONDS,
    )


async def _refresh_starrocks_credentials_forever(initial_lease_duration: int) -> None:
    sleep_for = _next_refresh_delay(initial_lease_duration)
    while True:
        await asyncio.sleep(sleep_for)
        try:
            user, password, lease_duration = await _resolve_starrocks_credentials()
            if lease_duration is None:
                # Only reachable if settings.vault_addr somehow became unset
                # after this loop started — it can't otherwise, since the
                # loop is only spawned when the initial fetch returned a
                # real lease. Treat it as a transient failure and retry
                # rather than crash the whole background task.
                msg = "Vault refresh returned no lease_duration"
                raise RuntimeError(msg)  # noqa: TRY301
            await starrocks_pool.rotate(user, password)
        except Exception:
            log.exception("Failed to refresh StarRocks Vault credentials; will retry")
            sleep_for = _CREDENTIAL_REFRESH_RETRY_SECONDS
            continue
        log.info("Rotated StarRocks pool onto freshly-issued Vault credentials")
        sleep_for = _next_refresh_delay(lease_duration)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    user, password, lease_duration = await _resolve_starrocks_credentials()
    await starrocks_pool.start(user, password)
    for tenant in TENANTS:
        if tenant.on_startup is not None:
            await tenant.on_startup()

    refresh_task = (
        asyncio.create_task(_refresh_starrocks_credentials_forever(lease_duration))
        if lease_duration is not None
        else None
    )
    try:
        yield
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
        for tenant in TENANTS:
            if tenant.on_shutdown is not None:
                await tenant.on_shutdown()
        await starrocks_pool.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="OL Analytics API",
        description="Multi-tenant read-only analytics gateway over StarRocks.",
        lifespan=lifespan,
    )

    add_request_logging(app)
    app.include_router(health.router)

    for tenant in TENANTS:
        # No add_request_logging() call on tenant.app here or in any
        # tenant's own app.py: BaseHTTPMiddleware wraps the whole ASGI call,
        # so root's middleware already sees the tenant's final response
        # (correct status code, full duration) for anything Starlette's
        # Mount delegates to it — adding the same middleware to the tenant
        # too would log every tenant request twice.
        app.mount(tenant.mount_path, tenant.app)

    return app


app = create_app()
