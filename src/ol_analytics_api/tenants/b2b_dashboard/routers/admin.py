"""MIT-admin-only analytics endpoints (contract health across all orgs).

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ol_analytics_api.core.db.query import build_select, fetch_and_suppress
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_dashboard.auth import require_mit_admin
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import (
    AdminAnalyticsResponse,
    MitAdminContractHealth,
)
from ol_analytics_api.tenants.b2b_dashboard.pagination import Pagination, pagination

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_mit_admin)],
)

# This endpoint spans all orgs, so the envelope carries no organization_key and
# the query has no org filter. ORDER BY (hardcoded, not caller input) makes
# LIMIT/OFFSET paging deterministic; pagination bounds the all-orgs result set.
_MV = "mv_b2b_mit_admin_contract_health"
_ORDER_BY = ("organization_key", "contract_pk")


@router.get("/contract-health")
async def contract_health(
    page: Annotated[Pagination, Depends(pagination)],
) -> AdminAnalyticsResponse[MitAdminContractHealth]:
    rows = await fetch_and_suppress(
        build_select(settings.starrocks_schema, _MV, MitAdminContractHealth, order_by=_ORDER_BY),
        (page.limit, page.offset),
        MitAdminContractHealth,
        settings.anonymization_floor,
    )
    return AdminAnalyticsResponse(
        # Per-MV freshness: this endpoint's own backing MV, not a schema-wide
        # MAX that a fresher, unrelated MV could inflate.
        as_of=await latest_refresh_timestamp(settings.starrocks_schema, _MV),
        data=rows,
    )
