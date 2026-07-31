"""Structured access-log middleware for the root app.

Granian's own access log is off by default (see the Dockerfile — it is never
enabled) in favor of this, so each request produces exactly one
structured JSON log line carrying method/path/status/duration plus whatever
trace_id/span_id/k8s context observability/processors.py injects.

Applied to the root app only, never to a tenant sub-app — Starlette's Mount
wraps a tenant's whole ASGI callable inside the root app's request
lifecycle, so this middleware already sees a tenant's final response for
anything delegated to it. Adding it to a tenant too would log every request
to that tenant twice.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response

log = structlog.get_logger("ol_analytics_api.access")


async def _log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        # An unhandled exception (or a cancelled/disconnected request) means
        # call_next() never returns a Response — without this, that request
        # would go completely unlogged instead of showing up as a failure.
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        log.exception(
            "request_failed",
            method=request.method,
            path=request.url.path,
            duration_ms=duration_ms,
        )
        raise
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    log.info(
        "request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response


def add_request_logging(app: FastAPI) -> None:
    app.middleware("http")(_log_requests)
