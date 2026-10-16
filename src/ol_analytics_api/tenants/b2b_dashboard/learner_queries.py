"""SQL for the contract-scoped learner endpoints: ``learner_progress`` (one row
per enrollment), ``learners`` (paged by learner), the needs-attention
aggregate and the course-runs list.

These rows identify individual learners, so they do not go through
core/db/query.py's anonymization chokepoint: a k-anonymity floor would
suppress every one of them. Org-manager authorization and the contract gate
still apply (routers/learners.py).

``needs_attention_aggregate`` is the one exception, and it goes the other way:
it returns no learner rows at all, only per-contract distinct-learner counts,
so it DOES go through that chokepoint and is floored like every other
aggregate in this tenant. It lives in this module rather than with the
materialized-view endpoints so that it can share ``_needs_attention`` with the
row-level queries above -- one predicate and one "today" for the KPI tile and
the learner directory alike.

The rules themselves are not here. ``completion_status`` and
``needs_attention_since`` are columns of the MV, defined once in
ol-data-platform's ``afact_learner_courserun_progress``, which the B2B views
also count from. This module only compares ``needs_attention_since`` to today.

Every query is a fixed template. The identifiers are this module's constants
plus the validated schema name, and every caller-supplied value is a bound
parameter. The only per-request variation is which of a fixed set of
predicates and orderings is included, and code chooses that, not input. That
is the justification for each ``# noqa: S608`` below.

The one spliced value is today's UTC date, and it is not caller-supplied: the
router reads it from StarRocks with ``UTC_TODAY_QUERY``, and ``_date_literal``
narrows it to a ``datetime.date`` before rendering, so the text it produces can
only ever be ``'YYYY-MM-DD'``. It is spliced rather than bound because it
appears in the SELECT list, which precedes the WHERE clause the existing
parameters bind to; binding it would mean prepending it to a tuple the page and
count queries share.

``learner_progress`` is two layers, as in the b2b_learner_records tenant. The
inner ``records`` select scopes to the organization and contract and renames
the MV's columns; the outer select applies consent, search and the status
filter, so the page and its count always share one WHERE clause. ``learners``
builds on the same ``records`` and the same WHERE clause and adds a paging
layer over learners; its docstring describes it.

Consent gates outcome fields, not the row. The MV carries the learner's
recorded decision for the contract as a nullable ``outcomes_shared`` (null =
none recorded), which ``records`` projects as ``outcomes_decision`` so the
outer alias ``outcomes_shared`` only ever means the resolved answer. A recorded
decision always wins; ``consent_fail_open`` covers learners with none. Both
that and ``outcomes_consent_status``, the unresolved decision, come from
core/consent.py, which the b2b_learner_records tenant shares.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ol_analytics_api.core.consent import CONSENT_STATUS_SQL, outcomes_shared_sql
from ol_analytics_api.core.db.identifiers import validate_sql_identifier
from ol_analytics_api.tenants.b2b_dashboard.config import settings

ENROLLMENT_MV = "mv_b2b_learner_enrollment"
CONTRACT_COURSERUN_MV = "mv_b2b_contract_courserun"

# needs_attention_since is defined against the UTC date, so ask for that
# explicitly rather than CURRENT_DATE(), which follows the session timezone.
UTC_TODAY_QUERY = "SELECT CAST(UTC_TIMESTAMP() AS DATE) AS today"


def _date_literal(value: object) -> str:
    """``value`` as a quoted SQL date literal, ``'YYYY-MM-DD'``.

    Splicing is safe only because this narrows to a real calendar date first:
    the rendered text comes from a ``datetime.date``, never from the input
    string. Anything it cannot read as a date raises rather than reaching the
    query -- see the module docstring.

    It accepts ``date``, ``datetime`` and an ISO-8601 string because the value
    crosses the DB driver, and which of those a DATE column arrives as depends
    on the driver and the StarRocks build. Rejecting the other two would turn
    a type surprise into a 500 on every request. ``datetime`` is truncated
    rather than passed through: it subclasses ``date``, so rendering its
    isoformat unchanged would emit ``YYYY-MM-DDTHH:MM:SS`` and change the
    comparison.

    Written bare rather than as ``DATE '...'`` so the expression parses in
    sqlite too, which is what lets the tests execute the production string
    verbatim instead of rewriting it. StarRocks casts an ISO-8601 literal to
    DATE to compare it against a DATE column.
    """
    if isinstance(value, datetime.datetime):
        value = value.date()
    elif isinstance(value, str):
        value = datetime.date.fromisoformat(value)
    if type(value) is not datetime.date:
        message = f"today must be a date, got {type(value).__name__}"
        raise TypeError(message)
    return f"'{value.isoformat()}'"


def _needs_attention(today: datetime.date) -> str:
    """Whether the row's ``needs_attention_since`` has arrived by ``today``.

    The rule is the MV's. ``needs_attention_since`` is the enrollment date for
    a learner who never started, 30 days after the last recorded activity for
    one still ``in_progress``, and NULL for ``passed``, ``certified`` and an
    ``in_progress`` row with a grade but no tracked activity. It follows the
    row's current status, so a learner whose certificate is revoked and who is
    not passing gets a date again. ol-data-platform's unit tests on
    ``afact_learner_courserun_progress`` pin each of those cases. What is left
    here is the comparison, and the ``<=`` in it is what includes the 30th
    quiet day: test_needs_attention_boundary_is_computed_from_real_rows pins
    that day.

    COALESCE makes the expression two-valued. A NULL ``needs_attention_since``
    compares as NULL, and all three readers share this expression:
    SUM(CASE WHEN ...) folds NULL into its ELSE, but an unguarded NULL in a
    WHERE clause is not FALSE, so such a row would fall out of
    ``needs_attention=true`` AND ``needs_attention=false`` alike, and project
    ``needs_attention: null`` on a row whose outcomes are shared.

    ``today`` is passed in rather than written into the SQL so that the page
    and count statements -- two round trips, and so two evaluations -- cannot
    straddle midnight and disagree about which learners are quiet. The caller
    resolves it once per request, as it already does for ``as_of``.
    """
    return f"COALESCE(needs_attention_since <= {_date_literal(today)}, FALSE)"


# Upstream stores "" rather than NULL for learners who never set a name. Null
# blank names so they sort with the missing ones instead of before every name.
_BLANK_AS_NULL_NAME = "NULLIF(TRIM(full_name), '')"

# The four values of the MV's completion_status. One per row, so these buckets
# never overlap; a row with withheld outcomes falls into none of them.
_STATUSES = ("not_started", "in_progress", "passed", "certified")

_COLUMNS = (
    "learner_id",
    "email",
    "full_name",
    "courserun_readable_id",
    "courserun_title",
    "courserun_start_on",
    "courserun_end_on",
    "enrolled_on",
    "enrollment_is_active",
    "enrollment_mode",
)
_OUTCOMES = (
    "completion_status",
    "is_passing",
    "grade",
    "letter_grade",
    "certificate_issued_on",
    "certificate_is_revoked",
    "last_active_on",
)


class SortKey(StrEnum):
    FULL_NAME = "full_name"
    EMAIL = "email"
    ENROLLED_ON = "enrolled_on"
    COURSERUN = "courserun_readable_id"


@dataclass(frozen=True)
class ProgressFilters:
    organization_id: str
    contract_id: int
    search: str | None = None
    completion_statuses: tuple[str, ...] = ()
    courserun_readable_id: str | None = None
    needs_attention: bool | None = None
    include_inactive: bool = False
    sort: SortKey = SortKey.FULL_NAME
    descending: bool = False


@dataclass(frozen=True)
class LearnerFilters:
    """``learners``' filters. Fewer than ``ProgressFilters``: the grid has one
    order, by name, and reads active enrollments only."""

    organization_id: str
    contract_id: int
    search: str | None = None
    completion_statuses: tuple[str, ...] = ()
    courserun_readable_ids: tuple[str, ...] = ()
    needs_attention: bool | None = None


@dataclass(frozen=True)
class ProgressQuery:
    """``params`` binds ``count``; ``page`` takes ``params`` plus LIMIT and OFFSET."""

    page: str
    count: str
    params: tuple[Any, ...]


@dataclass(frozen=True)
class _Records:
    """The inner ``records`` select, with the consent and needs-attention
    expressions read over it.

    Built once per query, so the row-level endpoint and the learner-level one
    compose the same select, the same consent rule and the same date, and
    cannot drift apart. ``params`` binds ``select``.
    """

    select: str
    params: tuple[Any, ...]
    shared: str
    needs_attention: str


def _outcomes_shared() -> str:
    return outcomes_shared_sql(fail_open=settings.consent_fail_open)


def _contains_pattern(term: str) -> str:
    """A LIKE pattern matching ``term`` anywhere, with its wildcards escaped so a
    search for ``50%`` matches the literal text."""
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _records(
    organization_id: str, contract_id: int, *, include_inactive: bool, today: datetime.date
) -> _Records:
    table = f"{validate_sql_identifier(settings.learner_records_schema)}.{ENROLLMENT_MV}"
    # The org predicate stays next to the contract one: contract ids are
    # globally unique but not secret.
    # shortcut: learners with no global_id (non-SSO accounts) are excluded because
    # learner_id is the grouping, paging and count key; expose user_pk if they must appear.
    scope = ["sso_organization_id = %s", "contract_id = %s", "user_global_id IS NOT NULL"]
    if not include_inactive:
        scope.append("enrollment_is_active = TRUE")
    select = (
        "SELECT user_pk, courserun_pk, user_global_id AS learner_id, email,"  # noqa: S608
        f" {_BLANK_AS_NULL_NAME} AS full_name,"
        " courserun_readable_id, courserun_title, courserun_start_on, courserun_end_on,"
        " enrollment_created_on AS enrolled_on, enrollment_is_active, enrollment_mode,"
        " completion_status, is_passing, grade_value AS grade,"
        " letter_grade, certificate_issued_on, certificate_is_revoked, last_active_on,"
        " needs_attention_since, outcomes_shared AS outcomes_decision"
        f" FROM {table} WHERE {' AND '.join(scope)}"
    )
    # One expression, one today, for the row field, the filter and the count.
    return _Records(
        select, (organization_id, contract_id), _outcomes_shared(), _needs_attention(today)
    )


def _where(
    records: _Records,
    *,
    search: str | None,
    completion_statuses: tuple[str, ...],
    needs_attention: bool | None,
    courserun_readable_ids: tuple[str, ...] = (),
) -> tuple[str, tuple[Any, ...]]:
    """The filter over ``records`` as `` WHERE ...`` (or empty), and its params.

    Every predicate tests one row, so filters combine on a single enrollment.
    """
    shared = records.shared
    predicates: list[str] = []
    params: list[Any] = []
    if search:
        pattern = _contains_pattern(search)
        predicates.append("(LOWER(email) LIKE %s OR LOWER(full_name) LIKE %s)")
        params.extend([pattern, pattern])
    if courserun_readable_ids:
        marks = ", ".join(["%s"] * len(courserun_readable_ids))
        # `=` for one run keeps /learner-progress's single-value SQL unchanged.
        predicates.append(
            "courserun_readable_id = %s"
            if len(courserun_readable_ids) == 1
            else f"courserun_readable_id IN ({marks})"
        )
        params.extend(courserun_readable_ids)
    if completion_statuses:
        # Status values match only rows whose outcomes are shared; `unknown`
        # selects the withheld ones. Otherwise a status filter would reveal the
        # status it is withholding.
        known = [value for value in completion_statuses if value != "unknown"]
        alternatives = []
        if known:
            alternatives.append(
                f"({shared} AND completion_status IN ({', '.join(['%s'] * len(known))}))"
            )
            params.extend(known)
        if "unknown" in completion_statuses:
            alternatives.append(f"NOT {shared}")
        predicates.append(f"({' OR '.join(alternatives)})")
    if needs_attention is not None:
        # Consent-gated like the status filter, and for the same reason: a
        # withheld row matches neither direction, so filtering can't reveal the
        # outcome the row is withholding. There is no `unknown` escape hatch
        # here as there is for status -- a bool has no third member -- so
        # withheld rows are reachable only by leaving this filter off.
        negate = "" if needs_attention else "NOT "
        predicates.append(f"({shared} AND {negate}({records.needs_attention}))")
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    return where, tuple(params)


def _projection(records: _Records) -> str:
    shared = records.shared
    return ", ".join(
        [
            *_COLUMNS,
            f"{CONSENT_STATUS_SQL} AS outcomes_consent_status",
            f"{shared} AS outcomes_shared",
            *(f"CASE WHEN {shared} THEN {name} END AS {name}" for name in _OUTCOMES),
            # Derived here rather than joining _OUTCOMES, which are columns:
            # this reads `records.needs_attention_since`, which the outer
            # select does not project. Same expression and same date
            # as the filter and the count, so the three cannot disagree about
            # which learners are quiet.
            f"CASE WHEN {shared} THEN {records.needs_attention} END AS needs_attention",
        ]
    )


def learner_progress(filters: ProgressFilters, today: datetime.date) -> ProgressQuery:
    records = _records(
        filters.organization_id,
        filters.contract_id,
        include_inactive=filters.include_inactive,
        today=today,
    )
    where, where_params = _where(
        records,
        search=filters.search,
        completion_statuses=filters.completion_statuses,
        courserun_readable_ids=(
            (filters.courserun_readable_id,) if filters.courserun_readable_id else ()
        ),
        needs_attention=filters.needs_attention,
    )
    shared = records.shared

    direction = "DESC" if filters.descending else "ASC"
    # Nulls last either way (full_name is often null, and `records` nulls blank
    # ones), then a unique tie-break so LIMIT/OFFSET paging is deterministic.
    order_by = (
        f"{filters.sort.value} IS NULL, {filters.sort.value} {direction}, user_pk, courserun_pk"
    )
    page = (
        f"SELECT {_projection(records)} FROM ({records.select}) records{where}"  # noqa: S608
        f" ORDER BY {order_by} LIMIT %s OFFSET %s"
    )
    status_sums = ", ".join(
        f"SUM(CASE WHEN {shared} AND completion_status = '{value}' THEN 1 ELSE 0 END) AS {value}"
        for value in _STATUSES
    )
    count = (
        "SELECT COUNT(*) AS total_count,"  # noqa: S608
        f" SUM(CASE WHEN {shared} THEN 0 ELSE 1 END) AS outcomes_withheld_count,"
        f" {status_sums},"
        f" SUM(CASE WHEN {shared} AND ({records.needs_attention}) THEN 1 ELSE 0 END)"
        " AS needs_attention_count"
        f" FROM ({records.select}) records{where}"
    )
    return ProgressQuery(page, count, (*records.params, *where_params))


# Each learner's sort key, nulls last: a blank name is already NULL in
# `records`, and MIN skips NULLs, so a learner sorts as nameless only when
# every row is. learner_id is the unique tie-break that makes paging
# deterministic.
_LEARNER_ORDER = (
    "page_full_name IS NULL, page_full_name, page_email IS NULL, page_email, page_learner_id"
)


def learners(filters: LearnerFilters, today: datetime.date) -> ProgressQuery:
    """Learners with at least one enrollment matching every filter, paged by
    learner, each with ALL of their active enrollments.

    In the page statement the filter runs only inside the ``matched``
    subquery, which ``paged`` orders and limits to pick the page of learners.
    The final select joins that page back to the unfiltered ``records``, so a
    matched learner's other enrollments come back too. The count applies the
    same filter to ``records`` directly, so ``total_count`` counts exactly the
    learners the pages walk through. ``_where`` tests one
    row at a time, so filters combine on a single enrollment: a learner with
    one passed enrollment and a different quiet one does not match
    ``passed`` plus ``needs_attention``.

    Revealing those other enrollments discloses nothing the filter didn't:
    consent is recorded per learner per contract, so all of a learner's rows
    share it, and the projection gates their outcomes exactly as on the
    row-level endpoint.

    ``records`` is one CTE, so its params bind once and the page still takes
    ``params`` plus LIMIT and OFFSET. ``paged`` is the only place with a LIMIT
    and is read once: a LIMITed CTE read twice could be evaluated twice and
    pick different learners. Its columns are renamed ``page_*`` so the
    projection's unqualified ``learner_id``, ``email`` and ``full_name`` stay
    unambiguous after the join. The ``matched`` subquery groups before
    ``paged`` orders, so the ORDER BY reads plain columns rather than
    aggregate aliases.
    """
    records = _records(
        filters.organization_id, filters.contract_id, include_inactive=False, today=today
    )
    where, where_params = _where(
        records,
        search=filters.search,
        completion_statuses=filters.completion_statuses,
        courserun_readable_ids=filters.courserun_readable_ids,
        needs_attention=filters.needs_attention,
    )
    page = (
        f"WITH records AS ({records.select}),"  # noqa: S608
        " paged AS ("
        "SELECT page_learner_id, page_full_name, page_email FROM ("
        "SELECT learner_id AS page_learner_id, MIN(full_name) AS page_full_name,"
        f" MIN(email) AS page_email FROM records{where} GROUP BY learner_id) matched"
        f" ORDER BY {_LEARNER_ORDER} LIMIT %s OFFSET %s)"
        f" SELECT {_projection(records)}"
        " FROM records JOIN paged ON records.learner_id = paged.page_learner_id"
        f" ORDER BY {_LEARNER_ORDER}, courserun_readable_id, courserun_pk"
    )
    count = (
        "SELECT COUNT(DISTINCT learner_id) AS total_count"  # noqa: S608
        f" FROM ({records.select}) records{where}"
    )
    return ProgressQuery(page, count, (*records.params, *where_params))


@dataclass(frozen=True)
class AggregateQuery:
    """``page`` takes ``params`` plus the anonymization floor, LIMIT and
    OFFSET; ``count`` takes ``params`` plus the floor."""

    page: str
    count: str
    params: tuple[Any, ...]


def needs_attention_aggregate(
    organization_id: str, contract_id: int | None, today: datetime.date
) -> AggregateQuery:
    """Distinct learners needing attention, grouped by contract.

    The aggregate behind the dashboard's needs-attention KPI tile. It reads the
    same learner-grain MV as ``learner_progress`` and reuses the same
    ``_needs_attention`` expression against the same per-request date, so the
    tile and the directory beneath it cannot disagree about which learners are
    quiet -- not even for a learner whose 30th quiet day is today.

    ``COUNT(DISTINCT CASE WHEN ... THEN learner_id END)`` is the point of the
    whole query. ``learner_progress``'s ``needs_attention_count`` is a
    ``SUM(CASE WHEN ...)`` over a learner x course-run grain, so a learner
    stale in three of the contract's courses counts three times. That cannot
    back a tile sitting beside ``active_learners``, which is itself a
    distinct-learner count: two adjacent tiles would read as learner counts
    while using different units. Here that learner counts once.

    ``contract_id`` narrows to one contract for the contract-scoped route;
    ``None`` returns one row per contract the organization holds, which is what
    the org-wide analytics view renders a card group from.
    """
    table = f"{validate_sql_identifier(settings.learner_records_schema)}.{ENROLLMENT_MV}"
    # The org predicate stays next to the contract one, as in learner_progress:
    # contract ids are globally unique but not secret.
    scope = ["sso_organization_id = %s"]
    params: list[Any] = [organization_id]
    if contract_id is not None:
        scope.append("contract_id = %s")
        params.append(contract_id)
    # Active enrollments only, with no caller override, and that is what makes
    # the tile and the directory count one population: learner_progress's
    # `include_inactive` defaults to false, so a manager clicking through from
    # the tile lands on exactly these learners.
    scope.append("enrollment_is_active = TRUE")
    records = (
        "SELECT user_global_id AS learner_id, contract_id, needs_attention_since,"  # noqa: S608
        " outcomes_shared AS outcomes_decision"
        f" FROM {table} WHERE {' AND '.join(scope)}"
    )

    shared = _outcomes_shared()
    needs_attention = _needs_attention(today)
    # learners_considered is deliberately NOT consent-gated and the other two
    # are, which mirrors learner_progress exactly: being enrolled is not an
    # outcome, so it is disclosed, while anything about a learner's progress is
    # gated. So the three do not sum -- a withheld learner is counted in the
    # first and the third, never the second -- and the third is what stops a
    # fail-closed stack from reading as "nobody needs attention".
    aggregates = (
        "COUNT(DISTINCT learner_id) AS learners_considered,"
        f" COUNT(DISTINCT CASE WHEN {shared} AND ({needs_attention}) THEN learner_id END)"
        " AS learners_needing_attention,"
        f" COUNT(DISTINCT CASE WHEN NOT {shared} THEN learner_id END)"
        " AS learners_outcomes_withheld"
    )
    # The primary-cohort gate, applied to the page AND the count, written once
    # so the two cannot drift. suppress_small_cohorts would drop a sub-floor row
    # anyway, but dropping it in Python AFTER the LIMIT is not equivalent:
    #
    #   - A page whose rows are all sub-floor comes back empty beside a positive
    #     total_count, and the visible contracts behind it are reachable only by
    #     guessing an offset. OrgAnalyticsResponse tells clients to compare
    #     total_count against len(data) plus offset, which that breaks.
    #   - Varying the offset and watching which contracts surface reveals how
    #     many suppressed ones precede each visible one -- exactly the count of
    #     sub-floor cohorts that gating the count query exists to withhold.
    #
    # Gating in SQL is what makes the page and the count describe one set.
    cohort_gate = "HAVING COUNT(DISTINCT learner_id) >= %s"
    # Grouping by the grain key makes it unique per row, so it is also a
    # deterministic ORDER BY for LIMIT/OFFSET paging.
    page = (
        f"SELECT contract_id, {aggregates} FROM ({records}) records"  # noqa: S608
        f" GROUP BY contract_id {cohort_gate}"
        " ORDER BY contract_id LIMIT %s OFFSET %s"
    )
    count = (
        "SELECT COUNT(*) AS total_count FROM ("  # noqa: S608
        f"SELECT contract_id FROM ({records}) records"
        f" GROUP BY contract_id {cohort_gate}) gated"
    )
    return AggregateQuery(page, count, tuple(params))


@dataclass(frozen=True)
class CourseRunsQuery:
    """``params`` binds ``count``; ``page`` takes ``params`` plus LIMIT and OFFSET."""

    page: str
    count: str
    params: tuple[Any, ...]


def course_runs(organization_id: str, contract_id: int) -> CourseRunsQuery:
    """The contract's course runs, for ``learner_progress``'s module filter.

    Catalog metadata, not learner rows: unlike ``learner_progress``, there is
    no consent gating and no anonymization floor, matching
    ``b2b_learner_records.queries.courses()`` for the same reason.
    """
    table = f"{validate_sql_identifier(settings.learner_records_schema)}.{CONTRACT_COURSERUN_MV}"
    where = "sso_organization_id = %s AND contract_id = %s"
    params: tuple[Any, ...] = (organization_id, contract_id)
    # Nulls last (self-paced runs have no start date), then title for a
    # human-friendly order, then the readable id as a unique tie-break so
    # LIMIT/OFFSET paging is deterministic even when runs share a title.
    page = (
        "SELECT courserun_readable_id AS courserun_id, courserun_title,"  # noqa: S608
        " courserun_start_on, courserun_end_on"
        f" FROM {table} WHERE {where}"
        " ORDER BY courserun_start_on IS NULL, courserun_start_on, courserun_title,"
        " courserun_readable_id"
        " LIMIT %s OFFSET %s"
    )
    count = f"SELECT COUNT(*) AS total_count FROM {table} WHERE {where}"  # noqa: S608
    return CourseRunsQuery(page, count, params)
