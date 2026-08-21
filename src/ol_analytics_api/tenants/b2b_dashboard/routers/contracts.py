"""Contract-scoped analytics endpoints.

The same five panels as ``organizations.py``, narrowed to one contract. This
mirrors MITx Online's manager dashboard, which nests contracts under an
organization (``manager/organizations/{org}/contracts/{contract}``) — the MIT
Learn dashboard follows a caller arriving from there, so it needs the same
addressing.

AUTHORIZATION IS UNCHANGED, and deliberately so. MITx Online's
``IsOrganizationManager`` authorizes at the ORGANIZATION level and merely
validates that the contract belongs to that org; there is no contract-manager
role. So these routes reuse ``require_org_manager`` verbatim and add
``require_contract_in_org`` for the belongs-to check. Should a contract admin
distinct from an org admin ever exist, it becomes a swap of the dependency at
this same path rather than a re-route.

Two of the five panels read contract-grained materialized views that exist
only for this router (mv_b2b_contract_{monthly_engagement_trend,
content_engagement_depth}); the other three read the same MVs as the org
endpoints, which carry a contract per row already.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import SQLModel

from ol_analytics_api.core.db.query import (
    build_count,
    build_select,
    fetch_and_suppress,
    fetch_visible_count,
)
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_dashboard.auth import (
    require_contract_in_org,
    require_org_manager,
)
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import (
    ContractContentEngagementDepth,
    ContractMonthlyEngagementTrend,
    ContractUtilization,
    EnrollmentCompletionFunnel,
    OrgAnalyticsResponse,
    ProgramFunnel,
)
from ol_analytics_api.tenants.b2b_dashboard.pagination import Pagination, pagination

router = APIRouter(
    prefix="/organizations/{organization_id}/contracts/{contract_id}",
    tags=["contracts"],
    # Order matters: the org-manager gate runs first, so a caller who manages
    # no part of this org never reaches the query that would tell them whether
    # a given contract exists.
    dependencies=[Depends(require_org_manager), Depends(require_contract_in_org)],
)

_SCHEMA = settings.starrocks_schema

# Both columns filter every query. The org predicate is not redundant next to
# the contract one: dropping it would let a manager of org A read org B's
# contract by naming its id, since contract ids are globally unique but not
# secret.
_FILTER_COLUMNS = ("sso_organization_id", "contract_id")


@dataclass(frozen=True)
class _ContractEndpoint:
    """One contract-scoped MV endpoint. Mirrors ``organizations._OrgEndpoint``,
    with ``order_by`` no longer needing to lead with the contract — every row
    in the result already belongs to the one contract in the path."""

    path: str
    mv: str
    model: type[SQLModel]
    order_by: tuple[str, ...]


ENDPOINTS: list[_ContractEndpoint] = [
    _ContractEndpoint(
        "/contract-utilization",
        "mv_b2b_contract_utilization",
        ContractUtilization,
        ("contract_pk",),
    ),
    _ContractEndpoint(
        "/enrollment-funnel",
        "mv_b2b_enrollment_completion_funnel",
        EnrollmentCompletionFunnel,
        ("courserun_pk",),
    ),
    _ContractEndpoint(
        "/engagement-trend",
        "mv_b2b_contract_monthly_engagement_trend",
        ContractMonthlyEngagementTrend,
        ("activity_year_and_month",),
    ),
    _ContractEndpoint(
        "/program-funnel",
        "mv_b2b_program_funnel",
        ProgramFunnel,
        ("program_pk",),
    ),
    _ContractEndpoint(
        "/content-engagement",
        "mv_b2b_contract_content_engagement_depth",
        ContractContentEngagementDepth,
        ("courserun_readable_id",),
    ),
]


def _register(spec: _ContractEndpoint) -> None:
    """Register one contract endpoint. Identical in shape to the org
    registration — query, suppress, wrap — differing only in the second bound
    filter value. Suppression, pagination and the as_of source are unchanged;
    the floor applies per row exactly as it does at org grain."""
    query = build_select(
        _SCHEMA, spec.mv, spec.model, filter_columns=_FILTER_COLUMNS, order_by=spec.order_by
    )
    count_query = build_count(_SCHEMA, spec.mv, spec.model, filter_columns=_FILTER_COLUMNS)

    async def endpoint(
        organization_id: str,
        contract_id: str,
        page: Annotated[Pagination, Depends(pagination)],
    ) -> OrgAnalyticsResponse[SQLModel]:
        rows = await fetch_and_suppress(
            query,
            (organization_id, contract_id, page.limit, page.offset),
            spec.model,
            settings.anonymization_floor,
        )
        return OrgAnalyticsResponse(
            organization_id=organization_id,
            as_of=await latest_refresh_timestamp(_SCHEMA, spec.mv),
            total_count=await fetch_visible_count(
                count_query,
                (organization_id, contract_id, settings.anonymization_floor),
            ),
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
        # See the same call in organizations.py: named explicitly so a
        # generated client's method name is ours rather than a derivative of
        # the path. The `contracts_` prefix is what separates these from the
        # org router's identically-named panels.
        operation_id=f"contracts_{endpoint.__name__}_retrieve",
    )


for _spec in ENDPOINTS:
    _register(_spec)
