"""MIT-admin-only analytics endpoints (contract health across all orgs).

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ol_analytics_api.core.db.query import fetch_and_suppress
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
    return await fetch_and_suppress(
        f"SELECT * FROM {settings.starrocks_schema}.mv_b2b_mit_admin_contract_health",  # noqa: S608
        (),
        MitAdminContractHealth,
        "seats_consumed",
        settings.anonymization_floor,
    )
