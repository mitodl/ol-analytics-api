"""Org-manager-scoped analytics endpoints.

Each of the 5 org-facing MVs is exposed as one GET, gated by the tenant's
require_org_manager dependency, suppressed below the anonymization floor,
and wrapped in the shared OrgAnalyticsResponse envelope (organization_key +
as_of + data) — see the Analytics API Endpoints epic.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlmodel import SQLModel

from ol_analytics_api.core.db.query import fetch_and_suppress
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
# field_validator) — safe to splice into query strings below.
_SCHEMA = settings.starrocks_schema


async def _org_response[RowT: SQLModel](  # noqa: PLR0913
    org_slug: str,
    base_query: str,
    params: tuple[Any, ...],
    model_cls: type[RowT],
    cohort_field: str,
    order_by: str,
    page: Pagination,
) -> OrgAnalyticsResponse[RowT]:
    """Run one MV query -> suppress -> wrap in the org envelope. The as_of
    lookup is shared across the 5 endpoints so a single response reports the
    same freshness the caller sees in every panel of the dashboard.

    ``order_by`` is a hardcoded column list per endpoint (never caller input)
    — it makes LIMIT/OFFSET paging deterministic, and is safe to splice.
    Pagination bounds the row count so a large org can't load its whole grain
    into the pod; suppression then runs over that bounded page."""
    query = f"{base_query} ORDER BY {order_by} LIMIT %s OFFSET %s"
    rows = await fetch_and_suppress(
        query,
        (*params, page.limit, page.offset),
        model_cls,
        cohort_field,
        settings.anonymization_floor,
    )
    return OrgAnalyticsResponse(
        organization_key=org_slug,
        as_of=await latest_refresh_timestamp(_SCHEMA),
        data=rows,
    )


@router.get("/contract-utilization")
async def contract_utilization(
    org_slug: str, page: Annotated[Pagination, Depends(pagination)]
) -> OrgAnalyticsResponse[ContractUtilization]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_contract_utilization WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ContractUtilization,
        "seats_consumed",
        "contract_pk",
        page,
    )


@router.get("/enrollment-funnel")
async def enrollment_funnel(
    org_slug: str, page: Annotated[Pagination, Depends(pagination)]
) -> OrgAnalyticsResponse[EnrollmentCompletionFunnel]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_enrollment_completion_funnel"  # noqa: S608
        " WHERE organization_key = %s",
        (org_slug,),
        EnrollmentCompletionFunnel,
        "enrolled_learners",
        "contract_pk, courserun_pk",
        page,
    )


@router.get("/engagement-trend")
async def engagement_trend(
    org_slug: str, page: Annotated[Pagination, Depends(pagination)]
) -> OrgAnalyticsResponse[MonthlyEngagementTrend]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_monthly_engagement_trend WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        MonthlyEngagementTrend,
        "monthly_active_learners",
        "activity_year_and_month",
        page,
    )


@router.get("/program-funnel")
async def program_funnel(
    org_slug: str, page: Annotated[Pagination, Depends(pagination)]
) -> OrgAnalyticsResponse[ProgramFunnel]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_program_funnel WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ProgramFunnel,
        "enrolled_in_contract_courses",
        "contract_pk, program_pk",
        page,
    )


@router.get("/content-engagement")
async def content_engagement(
    org_slug: str, page: Annotated[Pagination, Depends(pagination)]
) -> OrgAnalyticsResponse[ContentEngagementDepth]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_content_engagement_depth WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ContentEngagementDepth,
        "total_enrolled_learners",
        "courserun_readable_id",
        page,
    )
