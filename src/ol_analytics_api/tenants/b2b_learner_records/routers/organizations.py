"""The organization-scoped record collections.

Mounted at /api/v1/learner-records by tenants/b2b_learner_records/app.py.
"""

from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_learner_records import queries
from ol_analytics_api.tenants.b2b_learner_records.auth import require_organization_grant
from ol_analytics_api.tenants.b2b_learner_records.config import settings
from ol_analytics_api.tenants.b2b_learner_records.models import (
    CourseRun,
    Enrollment,
    Learner,
    LearnerRecordsResponse,
)

router = APIRouter(
    prefix="/organizations/{organization_id}",
    dependencies=[Depends(require_organization_grant)],
)


class CompletionStatusFilter(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    CERTIFIED = "certified"
    UNKNOWN = "unknown"


class Page(BaseModel):
    limit: int
    offset: int


def page(
    limit: Annotated[int, Query(ge=1, le=settings.max_page_size)] = settings.default_page_size,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    return Page(limit=limit, offset=offset)


PageParams = Annotated[Page, Depends(page)]
LearnerIds = Annotated[
    list[uuid.UUID] | None,
    Query(max_length=settings.max_learner_ids, description="Repeat for several."),
]
UpdatedSince = Annotated[
    datetime.datetime | None,
    Query(description="Return only records changed at or after this instant."),
]
IncludeInactive = Annotated[
    bool, Query(description="Include deactivated enrollments (unenrolled, refunded, transferred).")
]


async def _as_of(sources: tuple[str, ...]) -> datetime.datetime | None:
    # A response built from two views is only as fresh as the staler one.
    refreshed = [await latest_refresh_timestamp(settings.starrocks_schema, mv) for mv in sources]
    known = [timestamp for timestamp in refreshed if timestamp is not None]
    return min(known) if len(known) == len(refreshed) else None


async def _respond(
    query: queries.RecordQuery, organization_id: uuid.UUID, page: Page, model: type[BaseModel]
) -> LearnerRecordsResponse[BaseModel]:
    # Freshness first. A refresh landing between this and the record queries then
    # labels newer rows with the older as_of, so the next sync re-sends them
    # rather than skipping them.
    as_of = await _as_of(query.sources)
    rows = await starrocks_pool.fetch_all(query.page, (*query.params, page.limit, page.offset))
    counts = (await starrocks_pool.fetch_all(query.count, query.params))[0]
    return LearnerRecordsResponse(
        organization_id=organization_id,
        as_of=as_of,
        total_count=int(counts["total_count"]),
        # SUM over zero rows is NULL.
        outcomes_withheld_count=int(counts["outcomes_withheld_count"] or 0),
        data=[model(**row) for row in rows],
    )


@router.get(
    "/learners",
    operation_id="listLearners",
    tags=["learners"],
    response_model=LearnerRecordsResponse[Learner],
    summary="Learner roster with progress rollups",
)
async def list_learners(  # noqa: PLR0913
    *,
    organization_id: uuid.UUID,
    page: PageParams,
    contract_id: int | None = None,
    learner_id: LearnerIds = None,
    updated_since: UpdatedSince = None,
    include_inactive: IncludeInactive = False,
) -> LearnerRecordsResponse[BaseModel]:
    filters = queries.RecordFilters(
        organization_id=organization_id,
        contract_id=contract_id,
        learner_ids=tuple(learner_id or ()),
        updated_since=updated_since,
        include_inactive=include_inactive,
    )
    return await _respond(
        queries.learners(settings.starrocks_schema, filters), organization_id, page, Learner
    )


@router.get(
    "/enrollments",
    operation_id="listEnrollments",
    tags=["enrollments"],
    response_model=LearnerRecordsResponse[Enrollment],
    summary="Learner-by-course-run enrollment and completion records",
)
async def list_enrollments(  # noqa: PLR0913
    *,
    organization_id: uuid.UUID,
    page: PageParams,
    contract_id: int | None = None,
    courserun_id: str | None = None,
    learner_id: LearnerIds = None,
    completion_status: Annotated[list[CompletionStatusFilter] | None, Query()] = None,
    updated_since: UpdatedSince = None,
    include_inactive: IncludeInactive = False,
) -> LearnerRecordsResponse[BaseModel]:
    filters = queries.RecordFilters(
        organization_id=organization_id,
        contract_id=contract_id,
        courserun_id=courserun_id,
        learner_ids=tuple(learner_id or ()),
        completion_statuses=tuple(status.value for status in completion_status or ()),
        updated_since=updated_since,
        include_inactive=include_inactive,
    )
    return await _respond(
        queries.enrollments(settings.starrocks_schema, filters), organization_id, page, Enrollment
    )


@router.get(
    "/courses",
    operation_id="listCourses",
    tags=["catalog"],
    response_model=LearnerRecordsResponse[CourseRun],
    summary="Contracts and course runs covered by the organization's licence",
)
async def list_courses(
    *,
    organization_id: uuid.UUID,
    page: PageParams,
    contract_id: int | None = None,
) -> LearnerRecordsResponse[BaseModel]:
    filters = queries.RecordFilters(organization_id=organization_id, contract_id=contract_id)
    return await _respond(
        queries.courses(settings.starrocks_schema, filters), organization_id, page, CourseRun
    )
