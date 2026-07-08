"""Org-manager-scoped analytics endpoints.

Full endpoint design (filtering, pagination, error contracts) is tracked
under the Analytics API Endpoints epic; this router establishes the routing
+ auth + suppression shape for each of the 5 org-facing MVs.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ol_analytics_api.core.db.query import fetch_and_suppress
from ol_analytics_api.tenants.b2b_dashboard.auth import require_org_manager
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import (
    ContentEngagementDepth,
    ContractUtilization,
    EnrollmentCompletionFunnel,
    MonthlyEngagementTrend,
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


@router.get("/contract-utilization")
async def contract_utilization(org_slug: str) -> list[ContractUtilization]:
    return await fetch_and_suppress(
        f"SELECT * FROM {_SCHEMA}.mv_b2b_contract_utilization WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ContractUtilization,
        "seats_consumed",
        settings.anonymization_floor,
    )


@router.get("/enrollment-funnel")
async def enrollment_funnel(org_slug: str) -> list[EnrollmentCompletionFunnel]:
    return await fetch_and_suppress(
        f"SELECT * FROM {_SCHEMA}.mv_b2b_enrollment_completion_funnel"  # noqa: S608
        " WHERE organization_key = %s",
        (org_slug,),
        EnrollmentCompletionFunnel,
        "enrolled_learners",
        settings.anonymization_floor,
    )


@router.get("/engagement-trend")
async def engagement_trend(org_slug: str) -> list[MonthlyEngagementTrend]:
    return await fetch_and_suppress(
        f"SELECT * FROM {_SCHEMA}.mv_b2b_monthly_engagement_trend WHERE organization_key = %s"  # noqa: S608
        " ORDER BY activity_year_and_month",
        (org_slug,),
        MonthlyEngagementTrend,
        "monthly_active_learners",
        settings.anonymization_floor,
    )


@router.get("/program-funnel")
async def program_funnel(org_slug: str) -> list[ProgramFunnel]:
    return await fetch_and_suppress(
        f"SELECT * FROM {_SCHEMA}.mv_b2b_program_funnel WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ProgramFunnel,
        "enrolled_in_contract_courses",
        settings.anonymization_floor,
    )


@router.get("/content-engagement")
async def content_engagement(org_slug: str) -> list[ContentEngagementDepth]:
    return await fetch_and_suppress(
        f"SELECT * FROM {_SCHEMA}.mv_b2b_content_engagement_depth WHERE organization_key = %s",  # noqa: S608
        (org_slug,),
        ContentEngagementDepth,
        "total_enrolled_learners",
        settings.anonymization_floor,
    )
