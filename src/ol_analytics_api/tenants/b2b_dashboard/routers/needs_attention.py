"""Distinct-learner needs-attention counts, at org x contract grain.

Backs the needs-attention KPI tile on MIT Learn's B2B analytics page, beside
Seat utilization, Active learners and Completion rate. Those three come from
``mv_b2b_contract_utilization``; this one is aggregated here instead, over the
learner-grain MV the learner directory reads, so that the tile and the
directory share one needs-attention rule and one cutoff. See
``models.ContractNeedsAttention`` for why that was preferred to a dbt column.

Two routes, because the dashboard renders a card group per contract on both
views: the org route returns one row per contract the organization holds, and
the contract route returns the single row for one contract.

This is an aggregate, so unlike the learner-progress endpoint in the same
tenant it goes through core/db/query.py's anonymization chokepoint and carries
the k-anonymity floor. It has to: an unfloored small count sitting beside a
suppressed "—" in the next tile discloses by juxtaposition exactly what the
floor is there to hide.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.db.query import fetch_and_suppress, fetch_visible_count
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_dashboard import learner_queries
from ol_analytics_api.tenants.b2b_dashboard.auth import (
    require_contract_in_org,
    require_org_manager,
)
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import (
    ContractNeedsAttention,
    OrgAnalyticsResponse,
)
from ol_analytics_api.tenants.b2b_dashboard.pagination import Pagination, pagination

router = APIRouter(
    prefix="/organizations/{organization_id}",
    tags=["needs-attention"],
    dependencies=[Depends(require_org_manager)],
)

_SUMMARY = "Learners who may need a nudge, counted once each, per contract"


async def _respond(
    organization_id: str, contract_id: int | None, page: Pagination
) -> OrgAnalyticsResponse[ContractNeedsAttention]:
    """Resolve the cutoff, run the aggregate and its gated count, suppress, wrap.

    The cutoff is read from the cluster once per request and never cached, for
    the reason learner-progress does the same: the page and the count are two
    round trips, so leaving ``CURRENT_DATE()`` in the SQL would let them
    evaluate it either side of midnight and report a count the rows contradict.
    A cached cutoff would be wrong for exactly as long as the cache held it,
    which is worse than the bug — the whole value of the rule is being right at
    a date boundary. Resolving it here rather than in the query builder also
    means this endpoint and learner-progress agree within a request.
    """
    cutoff = (await starrocks_pool.fetch_all(learner_queries.NEEDS_ATTENTION_CUTOFF_QUERY))[0][
        "cutoff"
    ]
    query = learner_queries.needs_attention_aggregate(organization_id, contract_id, cutoff)
    # Freshness first, so a refresh landing mid-request labels newer rows with
    # the older as_of rather than the reverse. This is the learner-enrollment
    # MV's own refresh time, not contract-utilization's: a client showing this
    # tile beside that endpoint's three must read each one's own as_of.
    as_of = await latest_refresh_timestamp(
        settings.learner_records_schema, learner_queries.ENROLLMENT_MV
    )
    rows = await fetch_and_suppress(
        query.page,
        (*query.params, page.limit, page.offset),
        ContractNeedsAttention,
        settings.anonymization_floor,
    )
    return OrgAnalyticsResponse(
        organization_id=organization_id,
        as_of=as_of,
        total_count=await fetch_visible_count(
            query.count, (*query.params, settings.anonymization_floor)
        ),
        data=rows,
    )


@router.get(
    "/needs-attention",
    response_model=OrgAnalyticsResponse[ContractNeedsAttention],
    name="organization_needs_attention",
    operation_id="organizations_needs_attention_retrieve",
    summary=_SUMMARY,
)
async def organization_needs_attention(
    *, organization_id: str, page: Annotated[Pagination, Depends(pagination)]
) -> OrgAnalyticsResponse[ContractNeedsAttention]:
    return await _respond(organization_id, None, page)


@router.get(
    "/contracts/{contract_id}/needs-attention",
    response_model=OrgAnalyticsResponse[ContractNeedsAttention],
    name="contract_needs_attention",
    operation_id="contracts_needs_attention_retrieve",
    summary=_SUMMARY,
    # The org-manager gate on the router runs first, so a caller who manages no
    # part of this org never reaches the probe that would tell them whether a
    # given contract exists.
    dependencies=[Depends(require_contract_in_org)],
)
async def contract_needs_attention(
    *,
    organization_id: str,
    contract_id: int,
    page: Annotated[Pagination, Depends(pagination)],
) -> OrgAnalyticsResponse[ContractNeedsAttention]:
    return await _respond(organization_id, contract_id, page)
