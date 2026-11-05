"""The organization-scoped record collections.

Mounted at /api/v1/learner-records by tenants/b2b_learner_records/app.py.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Any

from cachetools import TTLCache
from fastapi import APIRouter, Depends, Query, status
from pydantic import AfterValidator, BaseModel

from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_learner_records import queries
from ol_analytics_api.tenants.b2b_learner_records.auth import require_organization_grant
from ol_analytics_api.tenants.b2b_learner_records.config import settings
from ol_analytics_api.tenants.b2b_learner_records.errors import ApiError, ErrorCode
from ol_analytics_api.tenants.b2b_learner_records.models import (
    CourseRun,
    Enrollment,
    Learner,
    LearnerRecordsResponse,
    RemovedEnrollment,
    RemovedLearner,
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
    list[uuid.UUID],
    Query(
        max_length=settings.max_learner_ids,
        description="Repeat for several.",
        default_factory=list,
    ),
]
_RETENTION_DAYS = queries.REMOVED_RECORD_RETENTION.days


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _within_retention(value: dt.datetime | None) -> dt.datetime | None:
    # Removed records are kept this long. An older cursor could have missed one,
    # and an upsert-only client would keep the record forever, so it reloads.
    if value is None:
        return value
    aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    if aware < _utc_now() - queries.REMOVED_RECORD_RETENTION:
        msg = (
            f"older than the {_RETENTION_DAYS} days removed records are kept; "
            "reload in full without updated_since"
        )
        raise ValueError(msg)
    return value


UpdatedSince = Annotated[
    dt.datetime | None,
    Query(
        description=(
            "Return only records changed at or after this instant, plus removed records. "
            f"At most {_RETENTION_DAYS} days ago, and not before change tracking began; "
            "an older value is a 400, and the client reloads in full."
        )
    ),
    AfterValidator(_within_retention),
]
UpdatedBefore = Annotated[
    dt.datetime | None,
    Query(
        description=(
            "Return only records changed strictly before this instant. Paired with "
            "updated_since, bounds a window for a partitioned backfill or replay."
        )
    ),
]
IncludeInactive = Annotated[
    bool, Query(description="Include deactivated enrollments (unenrolled, refunded, transferred).")
]


async def _as_of(sources: tuple[str, ...]) -> dt.datetime | None:
    # A response built from two views is only as fresh as the staler one.
    refreshed = [await latest_refresh_timestamp(settings.starrocks_schema, mv) for mv in sources]
    known = [timestamp for timestamp in refreshed if timestamp is not None]
    return min(known) if len(known) == len(refreshed) else None


# The start moves only when a log is restamped or recreated, so this holds the
# same staleness bound as as_of. For that long after a hand recreation, a cursor
# from before it is still accepted.
_LOG_START_CACHE_TTL_SECONDS = 300
_log_start_cache: TTLCache[tuple[str, str], dt.datetime | None] = TTLCache(
    maxsize=8, ttl=_LOG_START_CACHE_TTL_SECONDS
)


async def _change_log_start(log: str) -> dt.datetime | None:
    key = (settings.starrocks_schema, log)
    if key not in _log_start_cache:
        rows = await starrocks_pool.fetch_all(queries.change_log_start(*key))
        started: dt.datetime | None = rows[0]["started"]
        _log_start_cache[key] = started
    return _log_start_cache[key]


def clear_log_start_cache() -> None:
    """Test hook: the cache is process-global."""
    _log_start_cache.clear()


async def _require_change_log_coverage(
    change_logs: tuple[str, ...], updated_since: dt.datetime | None
) -> None:
    """Refuse a cursor older than a change log it reads.

    A record removed before the log existed (or before it was last recreated)
    has no row in it, so a sync from earlier would never learn of the removal.
    An empty log has no start to compare, so it refuses every cursor.
    """
    if updated_since is None:
        return
    since = updated_since if updated_since.tzinfo else updated_since.replace(tzinfo=dt.UTC)
    for log in change_logs:
        started = await _change_log_start(log)
        if started is None:
            began = "change tracking has no records yet"
        elif since < started.replace(tzinfo=dt.UTC):
            began = f"earlier than change tracking began ({started.isoformat()}Z)"
        else:
            continue
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.INVALID_PARAMETER,
            f"updated_since: {began}; reload in full without updated_since",
        )


Build = Callable[[dict[str, Any]], BaseModel]


def _record(model: type[BaseModel], removed: type[BaseModel]) -> Build:
    def build(row: dict[str, Any]) -> BaseModel:
        # The driver returns a BOOLEAN as 0 or 1, which Literal[False] refuses.
        is_removed = bool(row["removed"])
        return (removed if is_removed else model)(**{**row, "removed": is_removed})

    return build


async def _respond(
    query: queries.RecordQuery,
    organization_id: uuid.UUID,
    page: Page,
    build: Build,
    updated_since: dt.datetime | None = None,
) -> LearnerRecordsResponse[Any]:
    await _require_change_log_coverage(query.change_logs, updated_since)
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
        data=[build(row) for row in rows],
    )


@router.get(
    "/learners",
    operation_id="listLearners",
    tags=["learners"],
    response_model=LearnerRecordsResponse[Learner | RemovedLearner],
    summary="Learner roster with progress rollups",
)
async def list_learners(  # noqa: PLR0913
    *,
    organization_id: uuid.UUID,
    page: PageParams,
    contract_id: int | None = None,
    learner_id: LearnerIds,
    updated_since: UpdatedSince = None,
    updated_before: UpdatedBefore = None,
    include_inactive: IncludeInactive = False,
) -> LearnerRecordsResponse[Any]:
    filters = queries.RecordFilters(
        organization_id=organization_id,
        contract_id=contract_id,
        learner_ids=tuple(learner_id or ()),
        updated_since=updated_since,
        updated_before=updated_before,
        include_inactive=include_inactive,
    )
    return await _respond(
        queries.learners(settings.starrocks_schema, filters),
        organization_id,
        page,
        _record(Learner, RemovedLearner),
        updated_since,
    )


@router.get(
    "/enrollments",
    operation_id="listEnrollments",
    tags=["enrollments"],
    response_model=LearnerRecordsResponse[Enrollment | RemovedEnrollment],
    summary="Learner-by-course-run enrollment and completion records",
)
async def list_enrollments(  # noqa: PLR0913
    *,
    organization_id: uuid.UUID,
    page: PageParams,
    contract_id: int | None = None,
    courserun_id: str | None = None,
    learner_id: LearnerIds,
    completion_status: Annotated[list[CompletionStatusFilter], Query(default_factory=list)],
    updated_since: UpdatedSince = None,
    updated_before: UpdatedBefore = None,
    include_inactive: IncludeInactive = False,
) -> LearnerRecordsResponse[Any]:
    filters = queries.RecordFilters(
        organization_id=organization_id,
        contract_id=contract_id,
        courserun_id=courserun_id,
        learner_ids=tuple(learner_id or ()),
        completion_statuses=tuple(status.value for status in completion_status or ()),
        updated_since=updated_since,
        updated_before=updated_before,
        include_inactive=include_inactive,
    )
    return await _respond(
        queries.enrollments(settings.starrocks_schema, filters),
        organization_id,
        page,
        _record(Enrollment, RemovedEnrollment),
        updated_since,
    )


@router.get(
    "/courses",
    operation_id="listCourses",
    tags=["catalog"],
    response_model=LearnerRecordsResponse[CourseRun],
    summary="Contracts and course runs covered by the organization's licence",
)
async def list_courses(  # noqa: PLR0913
    *,
    organization_id: uuid.UUID,
    page: PageParams,
    contract_id: int | None = None,
    contract_is_active: bool | None = None,
    courserun_id: str | None = None,
    courserun_starts_after: Annotated[
        dt.datetime | None,
        Query(
            description=(
                "Return only course runs starting at or after this instant. Self-paced or "
                "otherwise unscheduled runs have no start date and match neither this nor "
                "courserun_starts_before; a partitioned sync needs one pass with neither "
                "set to pick those up."
            )
        ),
    ] = None,
    courserun_starts_before: Annotated[
        dt.datetime | None,
        Query(description="Return only course runs starting strictly before this instant."),
    ] = None,
) -> LearnerRecordsResponse[Any]:
    filters = queries.RecordFilters(
        organization_id=organization_id,
        contract_id=contract_id,
        contract_is_active=contract_is_active,
        courserun_id=courserun_id,
        courserun_starts_after=courserun_starts_after,
        courserun_starts_before=courserun_starts_before,
    )
    return await _respond(
        queries.courses(settings.starrocks_schema, filters),
        organization_id,
        page,
        lambda row: CourseRun(**row),
    )
