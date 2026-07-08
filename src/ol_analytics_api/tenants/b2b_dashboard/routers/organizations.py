"""Org-manager-scoped analytics endpoints.

Each of the 5 org-facing MVs is exposed as one GET, gated by the tenant's
require_org_manager dependency, suppressed below the anonymization floor,
and wrapped in the shared OrgAnalyticsResponse envelope (organization_key +
as_of + data) — see the Analytics API Endpoints epic.

The five endpoints are near-identical, so they're declared as a table of
``_OrgEndpoint`` specs and registered in a loop rather than hand-written five
times: the model<->MV<->ordering wiring is auditable at a glance, and the one
identifier-splicing construct lives once in ``build_select`` (no per-endpoint
``# noqa: S608``). Adding an org endpoint is one more row in ``ENDPOINTS``.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import SQLModel

from ol_analytics_api.core.db.query import build_select, fetch_and_suppress
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_dashboard.auth import require_org_manager
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import (
    ContentEngagementDepth,
    ContractUtilization,
    EnrollmentCompletionFunnel,
    MonthlyEngagementTrend,
    OrgAnalyticsResponse,
    ProgramFunnel,
)
from ol_analytics_api.tenants.b2b_dashboard.pagination import Pagination, pagination

router = APIRouter(
    prefix="/organizations/{org_slug}",
    tags=["organizations"],
    dependencies=[Depends(require_org_manager)],
)

# Validated as a safe SQL identifier at settings-load time (B2BDashboardSettings
# field_validator); build_select re-validates it and every other spliced token.
_SCHEMA = settings.starrocks_schema

# Every org query filters to the caller's org via a bound param, never a
# spliced value — the org_slug reaches StarRocks as %s, not as SQL text.
_ORG_FILTER_COLUMN = "organization_key"


@dataclass(frozen=True)
class _OrgEndpoint:
    """One org-scoped MV endpoint, declared instead of hand-written.

    ``order_by`` is a hardcoded column tuple per endpoint (never caller input)
    so LIMIT/OFFSET paging is deterministic; each column is validated as an
    identifier by ``build_select``.
    """

    path: str
    mv: str
    model: type[SQLModel]
    order_by: tuple[str, ...]


ENDPOINTS: list[_OrgEndpoint] = [
    _OrgEndpoint(
        "/contract-utilization",
        "mv_b2b_contract_utilization",
        ContractUtilization,
        ("contract_pk",),
    ),
    _OrgEndpoint(
        "/enrollment-funnel",
        "mv_b2b_enrollment_completion_funnel",
        EnrollmentCompletionFunnel,
        ("contract_pk", "courserun_pk"),
    ),
    _OrgEndpoint(
        "/engagement-trend",
        "mv_b2b_monthly_engagement_trend",
        MonthlyEngagementTrend,
        ("activity_year_and_month",),
    ),
    _OrgEndpoint(
        "/program-funnel",
        "mv_b2b_program_funnel",
        ProgramFunnel,
        ("contract_pk", "program_pk"),
    ),
    _OrgEndpoint(
        "/content-engagement",
        "mv_b2b_content_engagement_depth",
        ContentEngagementDepth,
        ("courserun_readable_id",),
    ),
]


def _register(spec: _OrgEndpoint) -> None:
    """Register one org endpoint: run its MV query -> suppress -> wrap in the
    org envelope. Pagination bounds the row count so a large org can't load its
    whole grain into the pod; suppression then runs over that bounded page.
    ``as_of`` is the backing MV's own last-refresh time, so the freshness a
    manager sees always matches the data in that panel — a different MV lagging
    its refresh can't make this endpoint look fresher than it is.

    Which columns the anonymization floor applies to is the row model's own
    ``cohort_policy`` — ``fetch_and_suppress`` reads it, so the spec names only
    the model, not a cohort field.
    """
    query = build_select(
        _SCHEMA, spec.mv, spec.model, filter_column=_ORG_FILTER_COLUMN, order_by=spec.order_by
    )

    async def endpoint(
        org_slug: str, page: Annotated[Pagination, Depends(pagination)]
    ) -> OrgAnalyticsResponse[SQLModel]:
        rows = await fetch_and_suppress(
            query,
            (org_slug, page.limit, page.offset),
            spec.model,
            settings.anonymization_floor,
        )
        return OrgAnalyticsResponse(
            organization_key=org_slug,
            as_of=await latest_refresh_timestamp(_SCHEMA, spec.mv),
            data=rows,
        )

    endpoint.__name__ = spec.path.lstrip("/").replace("-", "_")
    router.add_api_route(
        spec.path,
        endpoint,
        methods=["GET"],
        # Runtime-parametrized generic so each endpoint's OpenAPI schema carries
        # its concrete row model; mypy can't type a value used as a type param.
        response_model=OrgAnalyticsResponse[spec.model],  # type: ignore[name-defined]
        name=endpoint.__name__,
    )


for _spec in ENDPOINTS:
    _register(_spec)
