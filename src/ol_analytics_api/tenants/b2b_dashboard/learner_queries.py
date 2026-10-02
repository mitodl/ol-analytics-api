"""SQL for the contract-scoped learner-progress endpoint.

These rows identify individual learners, so they do not go through
core/db/query.py's anonymization chokepoint: a k-anonymity floor would
suppress every one of them. Org-manager authorization and the contract gate
still apply (routers/learners.py).

``needs_attention_aggregate`` is the one exception, and it goes the other way:
it returns no learner rows at all, only per-contract distinct-learner counts,
so it DOES go through that chokepoint and is floored like every other
aggregate in this tenant. It lives in this module rather than with the
materialized-view endpoints so that it can share ``_needs_attention`` and
``_COMPLETION_STATUS`` with the row-level queries above -- one rule and one
cutoff for the KPI tile and the learner directory alike.

Every query is a fixed template. The identifiers are this module's constants
plus the validated schema name, and every caller-supplied value is a bound
parameter. The only per-request variation is which of a fixed set of
predicates and orderings is included, and code chooses that, not input. That
is the justification for each ``# noqa: S608`` below.

The one spliced value is the needs-attention cutoff, and it is not
caller-supplied: the router reads it from StarRocks with
``NEEDS_ATTENTION_CUTOFF_QUERY``, and ``_date_literal`` narrows it to a
``datetime.date`` before rendering, so the text it produces can only ever be
``'YYYY-MM-DD'``. It is spliced rather than bound because it appears in the
SELECT list, which precedes the WHERE clause the existing parameters bind to;
binding it would mean prepending it to a tuple the page and count queries
share.

Each query is two layers, as in the b2b_learner_records tenant. The inner
``records`` select scopes to the organization and contract and derives
``completion_status``; the outer select applies consent, search and the status
filter, so the page and its count always share one WHERE clause.

Consent gates outcome fields, not the row. No consent field exists upstream
yet, so ``consent_fail_open`` picks the literal that decides every row. When
the field lands, ``_outcomes_shared`` becomes ``COALESCE(<field>, <literal>)``.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ol_analytics_api.core.db.identifiers import validate_sql_identifier
from ol_analytics_api.tenants.b2b_dashboard.config import settings

ENROLLMENT_MV = "mv_b2b_learner_enrollment"
CONTRACT_COURSERUN_MV = "mv_b2b_contract_courserun"

# Matches the b2b_learner_records tenant. An unrevoked certificate is certified
# without requiring is_passing, since production has unrevoked certificates with
# is_passing false (ol-data-platform#2669). in_progress must match
# mv_b2b_learner.courses_in_progress: a nonzero grade or any tracked activity
# (ol-data-platform#2693).
_COMPLETION_STATUS = (
    "CASE"
    " WHEN certificate_is_revoked = FALSE THEN 'certified'"
    " WHEN is_passing = TRUE THEN 'passed'"
    " WHEN grade_value > 0 OR last_active_on IS NOT NULL THEN 'in_progress'"
    " ELSE 'not_started'"
    " END"
)

NEEDS_ATTENTION_QUIET_DAYS = 30

# Resolves the cutoff on the StarRocks cluster, whose timezone is the one the
# rule is defined in -- not this process's, and not a browser's.
NEEDS_ATTENTION_CUTOFF_QUERY = (
    f"SELECT DATE_SUB(CURRENT_DATE(), INTERVAL {NEEDS_ATTENTION_QUIET_DAYS} DAY) AS cutoff"
)


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
        message = f"needs-attention cutoff must be a date, got {type(value).__name__}"
        raise TypeError(message)
    return f"'{value.isoformat()}'"


def _needs_attention(cutoff: datetime.date) -> str:
    """A learner needs attention if they never started, or if their last
    recorded activity was on or before ``cutoff`` (30 days before the
    cluster's today).

    ``<=`` is deliberate: "at least 30 days ago" includes the 30th day itself,
    and test_needs_attention_boundary_is_computed_from_real_rows pins that day.

    A NULL last_active_on on a non-not_started row (grade but no tracked
    activity) doesn't match the staleness branch -- there's no timestamp to
    judge quiet against. COALESCE settles that as "no" instead of NULL, which
    makes the expression two-valued. That matters because all three readers
    share it: SUM(CASE WHEN ...) already folded NULL into its ELSE, but an
    unguarded NULL in a WHERE clause is not FALSE, so such a row would fall out
    of ``needs_attention=true`` AND ``needs_attention=false`` alike, and
    project ``needs_attention: null`` on a row whose outcomes are shared.

    The cutoff is passed in rather than written as ``CURRENT_DATE()`` so that
    the page and count statements -- two round trips, and so two evaluations --
    cannot straddle midnight and disagree about which learners are quiet. The
    caller resolves it once per request, as it already does for ``as_of``.
    """
    return (
        "COALESCE("
        "completion_status = 'not_started'"
        f" OR last_active_on <= {_date_literal(cutoff)}"
        ", FALSE)"
    )


# Upstream stores "" rather than NULL for learners who never set a name. Null
# blank names so they sort with the missing ones instead of before every name.
_BLANK_AS_NULL_NAME = "NULLIF(TRIM(full_name), '')"

# The four branches of _COMPLETION_STATUS. Mutually exclusive, so these buckets
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
class ProgressQuery:
    """``params`` binds ``count``; ``page`` takes ``params`` plus LIMIT and OFFSET."""

    page: str
    count: str
    params: tuple[Any, ...]


def _outcomes_shared() -> str:
    return "TRUE" if settings.consent_fail_open else "FALSE"


def _contains_pattern(term: str) -> str:
    """A LIKE pattern matching ``term`` anywhere, with its wildcards escaped so a
    search for ``50%`` matches the literal text."""
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def learner_progress(filters: ProgressFilters, cutoff: datetime.date) -> ProgressQuery:
    table = f"{validate_sql_identifier(settings.learner_records_schema)}.{ENROLLMENT_MV}"
    # The org predicate stays next to the contract one: contract ids are
    # globally unique but not secret.
    scope = ["sso_organization_id = %s", "contract_id = %s"]
    params: list[Any] = [filters.organization_id, filters.contract_id]
    if not filters.include_inactive:
        scope.append("enrollment_is_active = TRUE")
    records = (
        "SELECT user_pk, courserun_pk, user_global_id AS learner_id, email,"  # noqa: S608
        f" {_BLANK_AS_NULL_NAME} AS full_name,"
        " courserun_readable_id, courserun_title, courserun_start_on, courserun_end_on,"
        " enrollment_created_on AS enrolled_on, enrollment_is_active, enrollment_mode,"
        f" {_COMPLETION_STATUS} AS completion_status, is_passing, grade_value AS grade,"
        " letter_grade, certificate_issued_on, certificate_is_revoked, last_active_on"
        f" FROM {table} WHERE {' AND '.join(scope)}"
    )

    shared = _outcomes_shared()
    # One expression, one cutoff, for the row field, the filter and the count.
    needs_attention = _needs_attention(cutoff)
    predicates: list[str] = []
    if filters.search:
        pattern = _contains_pattern(filters.search)
        predicates.append("(LOWER(email) LIKE %s OR LOWER(full_name) LIKE %s)")
        params.extend([pattern, pattern])
    if filters.courserun_readable_id:
        predicates.append("courserun_readable_id = %s")
        params.append(filters.courserun_readable_id)
    if filters.completion_statuses:
        # Status values match only rows whose outcomes are shared; `unknown`
        # selects the withheld ones. Otherwise a status filter would reveal the
        # status it is withholding.
        known = [value for value in filters.completion_statuses if value != "unknown"]
        alternatives = []
        if known:
            alternatives.append(
                f"({shared} AND completion_status IN ({', '.join(['%s'] * len(known))}))"
            )
            params.extend(known)
        if "unknown" in filters.completion_statuses:
            alternatives.append(f"NOT {shared}")
        predicates.append(f"({' OR '.join(alternatives)})")
    if filters.needs_attention is not None:
        # Consent-gated like the status filter, and for the same reason: a
        # withheld row matches neither direction, so filtering can't reveal the
        # outcome the row is withholding. There is no `unknown` escape hatch
        # here as there is for status -- a bool has no third member -- so
        # withheld rows are reachable only by leaving this filter off.
        negate = "" if filters.needs_attention else "NOT "
        predicates.append(f"({shared} AND {negate}({needs_attention}))")
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""

    direction = "DESC" if filters.descending else "ASC"
    # Nulls last either way (full_name is often null, and `records` nulls blank
    # ones), then a unique tie-break so LIMIT/OFFSET paging is deterministic.
    order_by = (
        f"{filters.sort.value} IS NULL, {filters.sort.value} {direction}, user_pk, courserun_pk"
    )
    projection = ", ".join(
        [
            *_COLUMNS,
            f"{shared} AS outcomes_shared",
            *(f"CASE WHEN {shared} THEN {name} END AS {name}" for name in _OUTCOMES),
            # Derived here rather than joining _OUTCOMES, which are columns:
            # this reads `records.completion_status`, an alias that only
            # resolves in this outer select. Same expression and same cutoff
            # as the filter and the count, so the three cannot disagree about
            # which learners are quiet.
            f"CASE WHEN {shared} THEN {needs_attention} END AS needs_attention",
        ]
    )
    page = (
        f"SELECT {projection} FROM ({records}) records{where}"  # noqa: S608
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
        f" SUM(CASE WHEN {shared} AND ({needs_attention}) THEN 1 ELSE 0 END)"
        " AS needs_attention_count"
        f" FROM ({records}) records{where}"
    )
    return ProgressQuery(page, count, tuple(params))


@dataclass(frozen=True)
class AggregateQuery:
    """``page`` takes ``params`` plus LIMIT and OFFSET; ``count`` takes
    ``params`` plus the anonymization floor."""

    page: str
    count: str
    params: tuple[Any, ...]


def needs_attention_aggregate(
    organization_id: str, contract_id: int | None, cutoff: datetime.date
) -> AggregateQuery:
    """Distinct learners needing attention, grouped by contract.

    The aggregate behind the dashboard's needs-attention KPI tile. It reads the
    same learner-grain MV as ``learner_progress`` and reuses the same
    ``_needs_attention`` expression against the same per-request cutoff, so the
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
        "SELECT user_global_id AS learner_id, contract_id,"  # noqa: S608
        f" {_COMPLETION_STATUS} AS completion_status, last_active_on"
        f" FROM {table} WHERE {' AND '.join(scope)}"
    )

    shared = _outcomes_shared()
    needs_attention = _needs_attention(cutoff)
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
    # Grouping by the grain key makes it unique per row, so it is also a
    # deterministic ORDER BY for LIMIT/OFFSET paging.
    page = (
        f"SELECT contract_id, {aggregates} FROM ({records}) records"  # noqa: S608
        " GROUP BY contract_id ORDER BY contract_id LIMIT %s OFFSET %s"
    )
    # The same primary-cohort gate build_count applies, for the same reason:
    # suppress_small_cohorts drops sub-floor rows after the query returns, so an
    # ungated COUNT would exceed anything paging can reach, and subtracting the
    # rows the caller does receive would tell them exactly how many sub-floor
    # contracts their org has.
    count = (
        "SELECT COUNT(*) AS total_count FROM ("  # noqa: S608
        f"SELECT contract_id FROM ({records}) records"
        " GROUP BY contract_id HAVING COUNT(DISTINCT learner_id) >= %s) gated"
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
