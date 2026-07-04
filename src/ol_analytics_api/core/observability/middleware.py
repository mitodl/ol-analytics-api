"""Structured access-log middleware, shared by the root app and every tenant.

uvicorn's own access logger is disabled (see main.py / Dockerfile — run with
`--no-access-log`) in favor of this, so each request produces exactly one
structured JSON log line carrying method/path/status/duration plus whatever
trace_id/span_id/k8s context observability/processors.py injects.
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
    response = await call_next(request)
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
