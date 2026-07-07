"""Org-manager-scoped analytics endpoints.

Each of the 5 org-facing MVs is exposed as one GET, gated by the tenant's
require_org_manager dependency, suppressed below the anonymization floor,
and wrapped in the shared OrgAnalyticsResponse envelope (organization_key +
as_of + data) — see the Analytics API Endpoints epic.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from typing import Any

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

router = APIRouter(
    prefix="/organizations/{org_slug}",
    tags=["organizations"],
    dependencies=[Depends(require_org_manager)],
)

# Validated as a safe SQL identifier at settings-load time (B2BDashboardSettings
# field_validator) — safe to splice into query strings below.
_SCHEMA = settings.starrocks_schema


async def _org_response[RowT: SQLModel](
    org_slug: str,
    query: str,
    params: tuple[Any, ...],
    model_cls: type[RowT],
    cohort_field: str,
) -> OrgAnalyticsResponse[RowT]:
    """Run one MV query -> suppress -> wrap in the org envelope. The as_of
    lookup is shared across the 5 endpoints so a single response reports the
    same freshness the caller sees in every panel of the dashboard."""
    rows = await fetch_and_suppress(
        query, params, model_cls, cohort_field, settings.anonymization_floor
    )
    return OrgAnalyticsResponse(
        organization_key=org_slug,
        as_of=await latest_refresh_timestamp(_SCHEMA),
        data=rows,
    )


@router.get("/contract-utilization")
async def contract_utilization(org_slug: str) -> OrgAnalyticsResponse[ContractUtilization]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_contract_utilization WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ContractUtilization,
        "seats_consumed",
    )


@router.get("/enrollment-funnel")
async def enrollment_funnel(org_slug: str) -> OrgAnalyticsResponse[EnrollmentCompletionFunnel]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_enrollment_completion_funnel"  # noqa: S608
        " WHERE organization_key = %s",
        (org_slug,),
        EnrollmentCompletionFunnel,
        "enrolled_learners",
    )


@router.get("/engagement-trend")
async def engagement_trend(org_slug: str) -> OrgAnalyticsResponse[MonthlyEngagementTrend]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_monthly_engagement_trend WHERE organization_key = %s"  # noqa: S608
        " ORDER BY activity_year_and_month",
        (org_slug,),
        MonthlyEngagementTrend,
        "monthly_active_learners",
    )


@router.get("/program-funnel")
async def program_funnel(org_slug: str) -> OrgAnalyticsResponse[ProgramFunnel]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_program_funnel WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ProgramFunnel,
        "enrolled_in_contract_courses",
    )


@router.get("/content-engagement")
async def content_engagement(org_slug: str) -> OrgAnalyticsResponse[ContentEngagementDepth]:
    return await _org_response(
        org_slug,
        f"SELECT * FROM {_SCHEMA}.mv_b2b_content_engagement_depth WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ContentEngagementDepth,
        "total_enrolled_learners",
    )
