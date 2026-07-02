"""MIT-admin-only analytics endpoints (contract health across all orgs)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ol_analytics_api.anonymization import suppress_small_cohorts
from ol_analytics_api.auth.dependencies import require_mit_admin
from ol_analytics_api.db.client import starrocks_pool
from ol_analytics_api.db.models import MitAdminContractHealth

router = APIRouter(
    prefix="/api/v1/analytics/admin",
    tags=["admin"],
    dependencies=[Depends(require_mit_admin)],
)


@router.get("/contract-health")
async def contract_health() -> list[MitAdminContractHealth]:
    rows = await starrocks_pool.fetch_all("SELECT * FROM mv_b2b_mit_admin_contract_health")
    return [MitAdminContractHealth(**row) for row in suppress_small_cohorts(rows, "seats_consumed")]
