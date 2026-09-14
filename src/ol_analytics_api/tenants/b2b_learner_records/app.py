"""B2B learner records tenant: identifiable per-learner progress for partners.

The opposite privacy posture from b2b_dashboard, which is why it is a separate
tenant rather than a router there (docs/b2b-learner-records-design.md §2).
Records here name individual learners. Access is machine-to-machine and
granted per contracted client (auth.py). Consent withholds outcome fields,
not records (models.py, queries.py).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ol_analytics_api.core.errors import add_shared_error_handlers
from ol_analytics_api.core.health import register_readiness_check
from ol_analytics_api.tenants.b2b_learner_records.routers import organizations

TENANT_NAME = "b2b_learner_records"


async def _bad_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # The contract's error body is {"detail": "<message>"} with a 400, not
    # FastAPI's 422 carrying a list of error objects.
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"][1:])
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": f"{location}: {error['msg']}"},
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="B2B Learner Records",
        description=(
            "Machine-to-machine, read-only, organization-scoped learner progress records "
            "for B2B site-license partners. Records identify individual learners."
        ),
    )
    app.include_router(organizations.router)
    add_shared_error_handlers(app)
    app.add_exception_handler(RequestValidationError, _bad_request)  # type: ignore[arg-type]
    register_readiness_check(TENANT_NAME)
    return app
