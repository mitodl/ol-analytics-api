"""The contract's error body: a machine-readable code beside the detail.

`docs/openapi/b2b-learner-records-v1.yaml` has always declared every error
response as `{code, detail}`, with `code` required and drawn from a fixed
enum, and told clients to branch on it because the wording of `detail` may
change. Nothing produced it: FastAPI's HTTPException renders `{"detail":
...}`, so a generated client validating against the spec would have rejected
every error this tenant returned.

The code can't be derived from the status alone, which is why this is an
exception type rather than a lookup in the handler: 403 is `missing_scope`
when the token's scopes fall short and `no_organization_access` when the
organization isn't granted, and those must stay distinct to a client without
being distinguishable to an attacker (both carry the same `detail`).

Scoped to this tenant. b2b_dashboard has no published contract to honour, so
adding a field to its error bodies would be a change with no reader.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ol_analytics_api.core.db.client import PoolAcquireTimeoutError


class ErrorCode(StrEnum):
    """The `code` enum in the contract's Error schema, verbatim."""

    INVALID_PARAMETER = "invalid_parameter"
    UNAUTHORIZED = "unauthorized"
    MISSING_SCOPE = "missing_scope"
    NO_ORGANIZATION_ACCESS = "no_organization_access"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


class ApiError(HTTPException):
    """An error this tenant's contract documents, carrying its code."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


def error_body(code: ErrorCode, detail: str) -> dict[str, str]:
    return {"code": str(code), "detail": detail}


async def _api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    error = exc if isinstance(exc, ApiError) else None
    if error is None:  # pragma: no cover - registered for ApiError only
        raise exc
    return JSONResponse(
        status_code=error.status_code,
        content=error_body(error.code, error.detail),
        headers=error.headers,
    )


async def _pool_acquire_timeout_handler(_request: Request, exc: Exception) -> JSONResponse:
    # Overrides the shared handler from core/errors.py, which returns the same
    # 503 without a code. Register it after add_shared_error_handlers.
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=error_body(ErrorCode.UNAVAILABLE, str(exc)),
        headers={"Retry-After": "1"},
    )


def add_error_handlers(app: FastAPI) -> None:
    """Render this tenant's documented errors in the contract's shape.

    Errors the contract doesn't document (a 404 for an unrouted path, a 405)
    keep FastAPI's own body. Dressing them in a code from the enum would
    claim a contract that doesn't cover them.
    """
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(PoolAcquireTimeoutError, _pool_acquire_timeout_handler)
