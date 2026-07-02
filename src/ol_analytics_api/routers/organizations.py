"""Org-manager-scoped analytics endpoints.

Full endpoint design (filtering, pagination, error contracts) is tracked
under the Analytics API Endpoints epic; this router establishes the routing
+ auth + suppression shape for each of the 5 org-facing MVs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ol_analytics_api.anonymization import suppress_small_cohorts
from ol_analytics_api.auth.dependencies import require_org_manager
from ol_analytics_api.db.client import starrocks_pool
from ol_analytics_api.db.models import (
    ContentEngagementDepth,
    ContractUtilization,
    EnrollmentCompletionFunnel,
    MonthlyEngagementTrend,
    ProgramFunnel,
)

router = APIRouter(
    prefix="/api/v1/analytics/organizations/{org_slug}",
    tags=["organizations"],
    dependencies=[Depends(require_org_manager)],
)


@router.get("/contract-utilization")
async def contract_utilization(org_slug: str) -> list[ContractUtilization]:
    rows = await starrocks_pool.fetch_all(
        "SELECT * FROM mv_b2b_contract_utilization WHERE organization_key = %s",
        (org_slug,),
    )
    return [ContractUtilization(**row) for row in suppress_small_cohorts(rows, "seats_consumed")]


@router.get("/enrollment-funnel")
async def enrollment_funnel(org_slug: str) -> list[EnrollmentCompletionFunnel]:
    rows = await starrocks_pool.fetch_all(
        "SELECT * FROM mv_b2b_enrollment_completion_funnel WHERE organization_key = %s",
        (org_slug,),
    )
    return [
        EnrollmentCompletionFunnel(**row)
        for row in suppress_small_cohorts(rows, "enrolled_learners")
    ]


@router.get("/engagement-trend")
async def engagement_trend(org_slug: str) -> list[MonthlyEngagementTrend]:
    rows = await starrocks_pool.fetch_all(
        "SELECT * FROM mv_b2b_monthly_engagement_trend WHERE organization_key = %s"
        " ORDER BY activity_year_and_month",
        (org_slug,),
    )
    return [
        MonthlyEngagementTrend(**row)
        for row in suppress_small_cohorts(rows, "monthly_active_learners")
    ]


@router.get("/program-funnel")
async def program_funnel(org_slug: str) -> list[ProgramFunnel]:
    rows = await starrocks_pool.fetch_all(
        "SELECT * FROM mv_b2b_program_funnel WHERE organization_key = %s",
        (org_slug,),
    )
    return [
        ProgramFunnel(**row) for row in suppress_small_cohorts(rows, "enrolled_in_contract_courses")
    ]


@router.get("/content-engagement")
async def content_engagement(org_slug: str) -> list[ContentEngagementDepth]:
    rows = await starrocks_pool.fetch_all(
        "SELECT * FROM mv_b2b_content_engagement_depth WHERE organization_key = %s",
        (org_slug,),
    )
    return [
        ContentEngagementDepth(**row)
        for row in suppress_small_cohorts(rows, "total_enrolled_learners")
    ]
