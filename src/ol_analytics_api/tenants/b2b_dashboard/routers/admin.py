"""MIT-admin-only analytics endpoints (contract health across all orgs).

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ol_analytics_api.core.db.query import fetch_and_suppress
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


@router.get("/contract-health")
async def contract_health(
    page: Annotated[Pagination, Depends(pagination)],
) -> AdminAnalyticsResponse[MitAdminContractHealth]:
    # settings.starrocks_schema is validated as a safe SQL identifier at
    # settings-load time (B2BDashboardSettings field_validator). This
    # endpoint spans all orgs, so the envelope carries no organization_key.
    # ORDER BY (hardcoded, not caller input) makes LIMIT/OFFSET paging
    # deterministic; pagination bounds the all-orgs result set.
    rows = await fetch_and_suppress(
        f"SELECT * FROM {settings.starrocks_schema}.mv_b2b_mit_admin_contract_health"  # noqa: S608
        " ORDER BY organization_key, contract_pk LIMIT %s OFFSET %s",
        (page.limit, page.offset),
        MitAdminContractHealth,
        "seats_consumed",
        settings.anonymization_floor,
    )
    return AdminAnalyticsResponse(
        as_of=await latest_refresh_timestamp(settings.starrocks_schema),
        data=rows,
    )
