"""Contract-scoped learner progress, as one row per learner per course run
(``/learner-progress``) or one row per learner with one enrollment per course
(``/learners``, whose courses ``/courses`` lists).

The only endpoints in this tenant that return individual learners, so they
waive the k-anonymity floor every other endpoint applies. An org manager is viewing
learners on seats their own organization purchased and assigned. Authorization
is the same as the contract endpoints: ``require_org_manager``, then
``require_contract_in_org``.

``/learner-progress`` reads ``mv_b2b_learner_enrollment`` and ``/learners``
reads ``mv_b2b_contract_member_enrollment``, which also lists contract members
with no active enrollment. Both are in the learner-records StarRocks database,
which is kept apart from the aggregate ``b2b_analytics`` views.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_dashboard import learner_queries
from ol_analytics_api.tenants.b2b_dashboard.auth import (
    NOT_LOADED_RESPONSES,
    require_contract_in_org,
    require_org_manager,
)
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.learner_models import (
    CompletionStatusCounts,
    Course,
    CourseRun,
    CourseRunsResponse,
    CoursesResponse,
    LearnerProgress,
    LearnerProgressResponse,
    LearnersResponse,
    LearnerWithEnrollments,
)
from ol_analytics_api.tenants.b2b_dashboard.pagination import (
    Pagination,
    learner_pagination,
    pagination,
)

router = APIRouter(
    prefix="/organizations/{organization_id}/contracts/{contract_id}",
    tags=["learners"],
    # The org-manager gate runs first, so a caller who manages no part of this
    # org never reaches the probe that would tell them whether a contract exists.
    dependencies=[Depends(require_org_manager), Depends(require_contract_in_org)],
    responses=NOT_LOADED_RESPONSES,
)


# CompletionStatus plus `unknown` for the withheld rows. A docstring here would
# render into the published spec as the parameter's description, so this stays a
# comment: needing attention is deliberately not a member, because it cuts
# across these buckets instead of partitioning them (a stale `in_progress` row
# is both). Folding it in would break the "exactly one bucket per row" property
# CompletionStatusCounts rests on, so it is the separate `needs_attention`
# filter below. It is a subset of `not_started` and `in_progress` and never
# selects rows currently classified as `passed` or `certified`, but that
# makes it no easier to express as a status: it still has to compose with
# whichever of those two a caller asked for.
class CompletionStatusFilter(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    CERTIFIED = "certified"
    UNKNOWN = "unknown"


# /learners' status filter: CompletionStatusFilter plus `not_enrolled`, which
# matches a missing enrollment, so /learner-progress, one row per enrollment,
# has nothing for it to select.
class LearnerStatusFilter(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    CERTIFIED = "certified"
    UNKNOWN = "unknown"
    NOT_ENROLLED = learner_queries.NOT_ENROLLED


SearchParam = Annotated[
    str | None,
    Query(min_length=1, max_length=254, description="Case-insensitive match on email or name."),
]
CompletionStatusParam = Annotated[
    list[CompletionStatusFilter],
    Query(
        description="Repeat for several. `unknown` selects rows with withheld outcomes.",
        default_factory=list,
    ),
]
LearnerStatusParam = Annotated[
    list[LearnerStatusFilter],
    Query(
        description=(
            "Repeat for several. `unknown` selects learners with withheld outcomes. "
            "`not_enrolled` selects learners missing an enrollment in at least one selected "
            "module, or in at least one of the contract's modules when none is selected."
        ),
        default_factory=list,
    ),
]

MAX_COURSERUN_FILTERS = 50
ModuleListParam = Annotated[
    list[Annotated[str, Query(min_length=1)]],
    Query(
        max_length=MAX_COURSERUN_FILTERS,
        description=(
            "Repeat for several modules; they OR together and AND with the other filters. "
            f"At most {MAX_COURSERUN_FILTERS}."
        ),
        default_factory=list,
    ),
]


async def _utc_today() -> dt.date:
    """Resolve today's UTC date on the cluster, once, before either data query.

    needs_attention_since is compared against it. The page and the count are
    two statements, so leaving the date in the SQL would let them evaluate it
    either side of midnight and disagree about which learners are quiet -- a
    row reading `false` while needs_attention_count counted it, and a
    `total_count` the page's own filter contradicts. Resolved per request and
    never cached: the failure this prevents is a date boundary, so a cached
    date would be wrong for exactly as long as the cache held it. This is the
    one non-deterministic expression in the tenant's SQL.
    """
    rows = await starrocks_pool.fetch_all(learner_queries.UTC_TODAY_QUERY)
    today: dt.date = rows[0]["today"]
    return today


async def _as_of(view: str) -> dt.datetime | None:
    # Read before the data queries, so a refresh landing mid-request labels
    # newer rows with the older as_of rather than the reverse.
    return await latest_refresh_timestamp(settings.learner_records_schema, view)


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
    search: SearchParam = None,
    completion_status: CompletionStatusParam,
    courserun_readable_id: Annotated[
        str | None,
        Query(
            min_length=1,
            description="Exact match. Narrows to one course run, e.g. the module filter.",
        ),
    ] = None,
    needs_attention: Annotated[
        bool | None,
        Query(
            description=(
                "Keep only the learners who need attention, or only those who don't. "
                "Cuts the same rows `needs_attention_count` counts. Rows with withheld "
                "outcomes match neither, so omit this to see them."
            )
        ),
    ] = None,
    include_inactive: Annotated[
        bool, Query(description="Include deactivated enrollments (unenrolled, refunded).")
    ] = False,
    sort: learner_queries.SortKey = learner_queries.SortKey.FULL_NAME,
    descending: bool = False,
) -> LearnerProgressResponse:
    today = await _utc_today()
    query = learner_queries.learner_progress(
        learner_queries.ProgressFilters(
            organization_id=organization_id,
            contract_id=contract_id,
            search=search,
            completion_statuses=tuple(status.value for status in completion_status),
            courserun_readable_id=courserun_readable_id,
            needs_attention=needs_attention,
            include_inactive=include_inactive,
            sort=sort,
            descending=descending,
        ),
        today,
    )
    as_of = await _as_of(learner_queries.ENROLLMENT_MV)
    rows = await starrocks_pool.fetch_all(query.page, (*query.params, page.limit, page.offset))
    counts = (await starrocks_pool.fetch_all(query.count, query.params))[0]
    return LearnerProgressResponse(
        organization_id=organization_id,
        as_of=as_of,
        total_count=int(counts["total_count"]),
        # SUM over zero rows is NULL.
        outcomes_withheld_count=int(counts["outcomes_withheld_count"] or 0),
        completion_status_counts=CompletionStatusCounts(
            not_started=int(counts["not_started"] or 0),
            in_progress=int(counts["in_progress"] or 0),
            passed=int(counts["passed"] or 0),
            certified=int(counts["certified"] or 0),
        ),
        needs_attention_count=int(counts["needs_attention_count"] or 0),
        data=[LearnerProgress(**row) for row in rows],
    )


@router.get(
    "/learners",
    response_model=LearnersResponse,
    name="learners",
    operation_id="learners_list",
    summary="Each learner under the contract with one enrollment per course, paged by learner",
    description=(
        "`enrollments` has one entry per course in `/courses`: when a learner has active "
        "enrollments in more than one run of a course, the run their own dashboard shows, "
        "and every field and filter reads that run. A run of a course `/courses` doesn't "
        "list has no entry. "
        "With no filter, every current member of the contract is listed, including those "
        "with no active enrollment, whose `enrollments` is empty. A member who enrolled but "
        "hasn't agreed to share their progress isn't listed, so `unknown` matches nobody "
        "here. Otherwise a learner is "
        "listed when at least one of their entries matches every filter at once, "
        "or, for `not_enrolled`, when they have no entry for at least one of the "
        "selected courses. "
        "Their other entries are returned too, whether or not they match. `limit` "
        "and `offset` count learners, not enrollments. `completion_status`, "
        "`course_readable_id`, `courserun_readable_id` and `needs_attention` narrow "
        "together: several values of one filter OR, and different filters AND on a single "
        "entry. `courserun_readable_id` matches the run an entry shows, and selects that "
        "run's course for `not_enrolled`. `needs_attention` "
        "never matches `not_enrolled`, which has no enrollment to judge."
    ),
)
async def learners(  # noqa: PLR0913
    *,
    organization_id: str,
    contract_id: int,
    page: Annotated[Pagination, Depends(learner_pagination)],
    search: SearchParam = None,
    completion_status: LearnerStatusParam,
    course_readable_id: ModuleListParam,
    courserun_readable_id: ModuleListParam,
    needs_attention: Annotated[
        bool | None,
        Query(
            description=(
                "Keep only learners with an enrollment that needs attention, or with one that "
                "doesn't. Rows with withheld outcomes match neither, so omit this to see them."
            )
        ),
    ] = None,
) -> LearnersResponse:
    today = await _utc_today()
    query = learner_queries.learners(
        learner_queries.LearnerFilters(
            organization_id=organization_id,
            contract_id=contract_id,
            search=search,
            completion_statuses=tuple(status.value for status in completion_status),
            courserun_readable_ids=tuple(courserun_readable_id),
            course_readable_ids=tuple(course_readable_id),
            needs_attention=needs_attention,
        ),
        today,
    )
    as_of = await _as_of(learner_queries.MEMBER_MV)
    rows = await starrocks_pool.fetch_all(query.page, (*query.params, page.limit, page.offset))
    counts = (await starrocks_pool.fetch_all(query.count, query.params))[0]
    return LearnersResponse(
        organization_id=organization_id,
        as_of=as_of,
        total_count=int(counts["total_count"]),
        outcomes_withheld_count=int(counts["outcomes_withheld_count"]),
        data=LearnerWithEnrollments.from_rows(rows),
    )


@router.get(
    "/course-runs",
    response_model=CourseRunsResponse,
    name="course_runs",
    operation_id="learners_course_runs_retrieve",
    summary="Course runs under the contract, for the learner-progress module filter",
)
async def course_runs(
    *,
    organization_id: str,
    contract_id: int,
    page: Annotated[Pagination, Depends(pagination)],
) -> CourseRunsResponse:
    query = learner_queries.course_runs(organization_id, contract_id)
    as_of = await latest_refresh_timestamp(
        settings.learner_records_schema, learner_queries.CONTRACT_COURSERUN_MV
    )
    rows = await starrocks_pool.fetch_all(query.page, (*query.params, page.limit, page.offset))
    counts = (await starrocks_pool.fetch_all(query.count, query.params))[0]
    return CourseRunsResponse(
        organization_id=organization_id,
        as_of=as_of,
        total_count=int(counts["total_count"]),
        data=[CourseRun(**row) for row in rows],
    )


@router.get(
    "/courses",
    response_model=CoursesResponse,
    name="courses",
    operation_id="learners_courses_retrieve",
    summary="Courses under the contract, in the learner dashboard's order, for the Learners page",
)
async def courses(
    *,
    organization_id: str,
    contract_id: int,
    page: Annotated[Pagination, Depends(pagination)],
) -> CoursesResponse:
    query = learner_queries.courses(organization_id, contract_id)
    as_of = await latest_refresh_timestamp(
        settings.learner_records_schema, learner_queries.CONTRACT_COURSERUN_MV
    )
    rows = await starrocks_pool.fetch_all(query.page, (*query.params, page.limit, page.offset))
    counts = (await starrocks_pool.fetch_all(query.count, query.params))[0]
    return CoursesResponse(
        organization_id=organization_id,
        as_of=as_of,
        total_count=int(counts["total_count"]),
        data=[Course(**row) for row in rows],
    )
