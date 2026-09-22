"""Contract-scoped learner progress: one row per learner per course run.

The only endpoint in this tenant that returns individual learners, so it waives
the k-anonymity floor every other endpoint applies. An org manager is viewing
learners on seats their own organization purchased and assigned. Authorization
is the same as the contract endpoints: ``require_org_manager``, then
``require_contract_in_org``.

Reads ``mv_b2b_learner_enrollment`` from the learner-records StarRocks
database, which is kept apart from the aggregate ``b2b_analytics`` views.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_dashboard import learner_queries
from ol_analytics_api.tenants.b2b_dashboard.auth import (
    require_contract_in_org,
    require_org_manager,
)
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.learner_models import (
    LearnerProgress,
    LearnerProgressResponse,
)
from ol_analytics_api.tenants.b2b_dashboard.pagination import Pagination, pagination

router = APIRouter(
    prefix="/organizations/{organization_id}/contracts/{contract_id}",
    tags=["learners"],
    # The org-manager gate runs first, so a caller who manages no part of this
    # org never reaches the probe that would tell them whether a contract exists.
    dependencies=[Depends(require_org_manager), Depends(require_contract_in_org)],
)


class CompletionStatusFilter(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    CERTIFIED = "certified"
    UNKNOWN = "unknown"


@router.get(
    "/learner-progress",
    response_model=LearnerProgressResponse,
    name="learner_progress",
    operation_id="learners_progress_retrieve",
    summary="Each learner's enrollment and completion in each course run under the contract",
)
async def learner_progress(  # noqa: PLR0913
    *,
    organization_id: str,
    contract_id: int,
    page: Annotated[Pagination, Depends(pagination)],
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=254, description="Case-insensitive match on email or name."),
    ] = None,
    completion_status: Annotated[
        list[CompletionStatusFilter] | None,
        Query(description="Repeat for several. `unknown` selects rows with withheld outcomes."),
    ] = None,
    include_inactive: Annotated[
        bool, Query(description="Include deactivated enrollments (unenrolled, refunded).")
    ] = False,
    sort: learner_queries.SortKey = learner_queries.SortKey.FULL_NAME,
    descending: bool = False,
) -> LearnerProgressResponse:
    query = learner_queries.learner_progress(
        learner_queries.ProgressFilters(
            organization_id=organization_id,
            contract_id=contract_id,
            search=search,
            completion_statuses=tuple(status.value for status in completion_status or ()),
            include_inactive=include_inactive,
            sort=sort,
            descending=descending,
        )
    )
    # Freshness first, so a refresh landing mid-request labels newer rows with
    # the older as_of rather than the reverse.
    as_of = await latest_refresh_timestamp(
        settings.learner_records_schema, learner_queries.ENROLLMENT_MV
    )
    rows = await starrocks_pool.fetch_all(query.page, (*query.params, page.limit, page.offset))
    counts = (await starrocks_pool.fetch_all(query.count, query.params))[0]
    return LearnerProgressResponse(
        organization_id=organization_id,
        as_of=as_of,
        total_count=int(counts["total_count"]),
        # SUM over zero rows is NULL.
        outcomes_withheld_count=int(counts["outcomes_withheld_count"] or 0),
        data=[LearnerProgress(**row) for row in rows],
    )
