"""FastAPI app factory for the B2B analytics service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ol_analytics_api.config import settings
from ol_analytics_api.db.client import starrocks_pool
from ol_analytics_api.db.vault_credentials import fetch_starrocks_credentials
from ol_analytics_api.routers import admin, organizations


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
        description="Read-only aggregated B2B site-license analytics for MIT Learn.",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["infra"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["infra"])
    async def ready() -> dict[str, str]:
        await starrocks_pool.ping()
        return {"status": "ready"}

    app.include_router(organizations.router)
    app.include_router(admin.router)

    return app


app = create_app()
