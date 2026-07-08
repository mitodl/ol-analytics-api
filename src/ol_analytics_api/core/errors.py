"""Maps shared-infra domain exceptions to HTTP responses.

Registered on each tenant's FastAPI sub-app (a mounted sub-app handles its
own exceptions — a handler on the root app never sees exceptions raised
inside a Mount), so every tenant that reads through the shared StarRocks
pool turns a saturated-pool timeout into a fast 503 instead of a 500.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from ol_analytics_api.core.db.client import PoolAcquireTimeoutError


async def _pool_acquire_timeout_handler(
    _request: Request, exc: PoolAcquireTimeoutError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
        headers={"Retry-After": "1"},
    )


def add_shared_error_handlers(app: FastAPI) -> None:
    """Wire the shared-infra exception handlers onto a tenant sub-app."""
    app.add_exception_handler(PoolAcquireTimeoutError, _pool_acquire_timeout_handler)  # type: ignore[arg-type]
