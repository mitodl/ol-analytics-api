"""MIT-admin-only analytics endpoints (contract health across all orgs).

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ol_analytics_api.core.anonymization import suppress_small_cohorts
from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.tenants.b2b_dashboard.auth import require_mit_admin
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import MitAdminContractHealth

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_mit_admin)],
)


@router.get("/contract-health")
async def contract_health() -> list[MitAdminContractHealth]:
    # settings.starrocks_schema is validated as a safe SQL identifier at
    # settings-load time (B2BDashboardSettings field_validator).
    rows = await starrocks_pool.fetch_all(
        f"SELECT * FROM {settings.starrocks_schema}.mv_b2b_mit_admin_contract_health"  # noqa: S608
    )
    return [
        MitAdminContractHealth(**row)
        for row in suppress_small_cohorts(rows, "seats_consumed", settings.anonymization_floor)
    ]
