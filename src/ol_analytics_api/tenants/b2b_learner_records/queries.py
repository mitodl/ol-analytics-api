"""SQL for the learner-records collections.

Every query is a fixed template. The identifiers are this module's constants
plus the tenant's validated schema name, and every caller-supplied value is a
bound parameter. The only per-request variation is which of a fixed set of
predicates is included, and code chooses that, not input. That is the
justification for each ``# noqa: S608`` below.

Each query is two layers. The inner ``records`` select renames MV columns to
the API's names and carries two internal columns, ``user_pk`` (a paging
tie-break, never returned) and ``record_updated_on`` (the ``updated_since``
cursor). The outer select applies consent, the cross-collection filters,
ordering and paging, so the page and its count always share one WHERE clause.

Consent is enforced here. ``_outcomes_shared()`` returns the SQL expression
deciding whether a record's outcomes may be disclosed. No consent field exists
upstream yet, so it is a literal chosen by ``consent_fail_open``: FALSE by
default, which fails closed and nulls every outcome column, or TRUE where a
deployment opts to fail open. When the field lands, the inner selects project
it and this becomes ``COALESCE(<consent column>, <that literal>)``, so a
recorded decision always wins and the setting only covers learners with none.

Columns the warehouse doesn't carry yet (consent date, activity) are projected
as NULL by the inner selects, so filling one in touches the inner select only.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from ol_analytics_api.core.db.identifiers import validate_sql_identifier
from ol_analytics_api.tenants.b2b_learner_records.config import settings

LEARNER_MV = "mv_b2b_learner"
ENROLLMENT_MV = "mv_b2b_learner_enrollment"


def _outcomes_shared() -> str:
    return "TRUE" if settings.consent_fail_open else "FALSE"


# An unrevoked certificate is certified without requiring is_passing: production
# has enrollments with an unrevoked certificate and is_passing false
# (ol-data-platform#2669). A revoked certificate falls through to the grade.
# Until activity data lands, "in progress" can only mean a nonzero grade.
_COMPLETION_STATUS = (
    "CASE"
    " WHEN certificate_is_revoked = FALSE THEN 'certified'"
    " WHEN is_passing = TRUE THEN 'passed'"
    " WHEN grade_value > 0 THEN 'in_progress'"
    " ELSE 'not_started'"
    " END"
)

_LEARNER_COLUMNS = (
    "learner_id",
    "email",
    "full_name",
    "organization_id",
    "organization_name",
    "membership_source",
    "is_organization_manager",
    "first_enrolled_on",
    "last_enrolled_on",
    "courses_enrolled",
)
_LEARNER_OUTCOMES = (
    "outcomes_consent_on",
    "last_active_on",
    "courses_in_progress",
    "courses_passed",
    "courses_certified",
    "certificates_earned",
)
_LEARNER_PENDING = (
    " NULL AS outcomes_consent_on, NULL AS last_active_on, NULL AS courses_in_progress,"
)

_ENROLLMENT_COLUMNS = (
    "learner_id",
    "email",
    "full_name",
    "organization_id",
    "contract_id",
    "contract_name",
    "courserun_id",
    "courserun_title",
    "courserun_start_on",
    "courserun_end_on",
    "enrolled_on",
    "enrollment_is_active",
    "enrollment_mode",
    "enrollment_status",
)
_ENROLLMENT_OUTCOMES = (
    "completion_status",
    "is_passing",
    "grade",
    "letter_grade",
    "certificate_issued_on",
    "certificate_is_revoked",
    "last_active_on",
    "days_active",
    "videos_watched",
    "problems_attempted",
    "chatbot_interactions",
)
_ENROLLMENT_PENDING = (
    " NULL AS last_active_on, NULL AS days_active, NULL AS videos_watched,"
    " NULL AS problems_attempted, NULL AS chatbot_interactions,"
)


@dataclass(frozen=True)
class RecordFilters:
    organization_id: uuid.UUID
    contract_id: int | None = None
    courserun_id: str | None = None
    learner_ids: tuple[uuid.UUID, ...] = ()
    completion_statuses: tuple[str, ...] = ()
    updated_since: datetime.datetime | None = None
    include_inactive: bool = False


@dataclass(frozen=True)
class RecordQuery:
    """``params`` binds ``count``; ``page`` takes ``params`` plus LIMIT and OFFSET.

    ``sources`` names the MVs read, for the envelope's ``as_of``."""

    page: str
    count: str
    params: tuple[Any, ...]
    sources: tuple[str, ...]


def _placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


def _cursor_value(value: datetime.datetime) -> str:
    """Render ``updated_since`` for comparison against ``record_updated_on``.

    The MVs store that cursor as a zone-less UTC ISO-8601 string, so the
    comparison is lexicographic. Truncating to whole seconds makes the bound a
    prefix of any stored value in the same second, whatever its fractional
    precision, so a record at the boundary is re-sent rather than skipped.
    """
    if value.tzinfo is not None:
        value = value.astimezone(datetime.UTC).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat()


def _assemble(  # noqa: PLR0913
    records: str,
    record_params: list[Any],
    columns: tuple[str, ...],
    outcomes: tuple[str, ...],
    order_by: tuple[str, ...],
    predicates: list[str],
    predicate_params: list[Any],
    sources: tuple[str, ...],
) -> RecordQuery:
    shared = _outcomes_shared()
    projection = ", ".join(
        [
            *columns,
            f"{shared} AS outcomes_shared",
            *(f"CASE WHEN {shared} THEN {name} END AS {name}" for name in outcomes),
        ]
    )
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    page = (
        f"SELECT {projection} FROM ({records}) records{where}"  # noqa: S608
        f" ORDER BY {', '.join(order_by)} LIMIT %s OFFSET %s"
    )
    count = (
        "SELECT COUNT(*) AS total_count,"  # noqa: S608
        f" SUM(CASE WHEN {shared} THEN 0 ELSE 1 END) AS outcomes_withheld_count"
        f" FROM ({records}) records{where}"
    )
    return RecordQuery(page, count, (*record_params, *predicate_params), sources)


def _shared_predicates(filters: RecordFilters) -> tuple[list[str], list[Any]]:
    predicates: list[str] = []
    params: list[Any] = []
    if filters.learner_ids:
        predicates.append(f"learner_id IN ({_placeholders(len(filters.learner_ids))})")
        params.extend(str(learner_id) for learner_id in filters.learner_ids)
    if filters.updated_since is not None:
        predicates.append("record_updated_on >= %s")
        params.append(_cursor_value(filters.updated_since))
    return predicates, params


def enrollments(schema: str, filters: RecordFilters) -> RecordQuery:
    table = f"{validate_sql_identifier(schema)}.{ENROLLMENT_MV}"
    scope = ["sso_organization_id = %s"]
    scope_params: list[Any] = [str(filters.organization_id)]
    if not filters.include_inactive:
        scope.append("enrollment_is_active = TRUE")
    if filters.contract_id is not None:
        scope.append("contract_id = %s")
        scope_params.append(filters.contract_id)
    if filters.courserun_id is not None:
        scope.append("courserun_readable_id = %s")
        scope_params.append(filters.courserun_id)
    records = (
        "SELECT user_pk, user_global_id AS learner_id, email, full_name,"  # noqa: S608
        " sso_organization_id AS organization_id, contract_id,"
        " b2b_contract_name AS contract_name, courserun_readable_id AS courserun_id,"
        " courserun_title, courserun_start_on, courserun_end_on,"
        " enrollment_created_on AS enrolled_on, enrollment_is_active, enrollment_mode,"
        f" enrollment_status, {_COMPLETION_STATUS} AS completion_status, is_passing,"
        " grade_value AS grade, letter_grade, certificate_issued_on, certificate_is_revoked,"
        f"{_ENROLLMENT_PENDING} record_updated_on"
        f" FROM {table} WHERE {' AND '.join(scope)}"
    )

    predicates, params = _shared_predicates(filters)
    if filters.completion_statuses:
        # Status values match only records whose outcomes are shared; `unknown`
        # selects the withheld ones. Otherwise a status filter would reveal the
        # status it is withholding.
        shared = _outcomes_shared()
        known = [value for value in filters.completion_statuses if value != "unknown"]
        alternatives = []
        if known:
            alternatives.append(
                f"({shared} AND completion_status IN ({_placeholders(len(known))}))"
            )
            params.extend(known)
        if "unknown" in filters.completion_statuses:
            alternatives.append(f"NOT {shared}")
        predicates.append(f"({' OR '.join(alternatives)})")

    return _assemble(
        records,
        scope_params,
        _ENROLLMENT_COLUMNS,
        _ENROLLMENT_OUTCOMES,
        ("learner_id", "contract_id", "courserun_id", "user_pk"),
        predicates,
        params,
        (ENROLLMENT_MV,),
    )


def learners(schema: str, filters: RecordFilters) -> RecordQuery:
    """The learner rollup.

    ``mv_b2b_learner`` precomputes it across all the organization's contracts,
    which serves the default request. ``courses_enrolled``, the enrolled_on
    dates and ``both`` membership count active enrollments. Completions and the
    cursor count every enrollment, so a learner whose seats were all reclaimed
    keeps a row with ``courses_enrolled = 0`` and the completions already sent.
    A ``contract_id`` or ``include_inactive`` request recomputes the rollup from
    ``mv_b2b_learner_enrollment`` on the same definitions and joins it back to
    ``mv_b2b_learner`` for roster membership and program certificates.
    """
    schema = validate_sql_identifier(schema)
    if filters.contract_id is None and not filters.include_inactive:
        records = (
            "SELECT user_pk, user_global_id AS learner_id, email, full_name,"  # noqa: S608
            " sso_organization_id AS organization_id, organization_name, membership_source,"
            " is_organization_manager, first_enrolled_on, last_enrolled_on, courses_enrolled,"
            " courses_passed, courses_certified,"
            " courses_certified + program_certificates_earned AS certificates_earned,"
            f"{_LEARNER_PENDING} record_updated_on"
            f" FROM {schema}.{LEARNER_MV} WHERE sso_organization_id = %s"
        )
        record_params: list[Any] = [str(filters.organization_id)]
        sources: tuple[str, ...] = (LEARNER_MV,)
    else:
        records, record_params = _recomputed_learners(schema, filters)
        sources = (LEARNER_MV, ENROLLMENT_MV)

    predicates, params = _shared_predicates(filters)
    return _assemble(
        records,
        record_params,
        _LEARNER_COLUMNS,
        _LEARNER_OUTCOMES,
        ("learner_id", "user_pk"),
        predicates,
        params,
        sources,
    )


def _recomputed_learners(schema: str, filters: RecordFilters) -> tuple[str, list[Any]]:
    scope = ["sso_organization_id = %s"]
    params: list[Any] = [str(filters.organization_id), str(filters.organization_id)]
    if filters.contract_id is not None:
        scope.append("contract_id = %s")
        params.append(filters.contract_id)

    # The same definitions as mv_b2b_learner (ol-data-platform#2669). Only
    # courses_enrolled and the enrolled_on dates look at enrollment_is_active, and
    # only without include_inactive. Completions and the cursor span every
    # enrollment, so a filtered request can't report fewer completions than the
    # default one, and deactivation moves the cursor forward.
    if filters.include_inactive:
        enrolled_run = "courserun_pk"
        enrolled_on = "enrollment_created_on"
    else:
        enrolled_run = "CASE WHEN enrollment_is_active = TRUE THEN courserun_pk END"
        enrolled_on = "CASE WHEN enrollment_is_active = TRUE THEN enrollment_created_on END"

    if filters.contract_id is not None:
        # Learners with any enrollment in the contract, active or not, as the
        # default rollup keeps learners whose seats were reclaimed. A program
        # certificate has no course run and so belongs to no one contract; it is
        # left out, matching "every rollup from that contract's enrollments only".
        join = "RIGHT JOIN"
        program_certificates = "0"
        record_updated_on = "e.record_updated_on"
    else:
        # include_inactive: every roster member, plus anyone enrolled at all.
        join = "FULL OUTER JOIN"
        program_certificates = "COALESCE(l.program_certificates_earned, 0)"
        record_updated_on = (
            "CASE WHEN l.record_updated_on IS NULL THEN e.record_updated_on"
            " WHEN e.record_updated_on IS NULL THEN l.record_updated_on"
            " ELSE GREATEST(l.record_updated_on, e.record_updated_on) END"
        )

    enrollment_rollup = (
        "SELECT user_pk, MAX(user_global_id) AS user_global_id, MAX(email) AS email,"  # noqa: S608
        " MAX(full_name) AS full_name, MAX(sso_organization_id) AS sso_organization_id,"
        " MAX(organization_name) AS organization_name,"
        f" MIN({enrolled_on}) AS first_enrolled_on,"
        f" MAX({enrolled_on}) AS last_enrolled_on,"
        f" COUNT(DISTINCT {enrolled_run}) AS courses_enrolled,"
        " COUNT(DISTINCT CASE WHEN is_passing = TRUE THEN courserun_pk END) AS courses_passed,"
        " COUNT(DISTINCT CASE WHEN certificate_is_revoked = FALSE THEN courserun_pk END)"
        " AS courses_certified,"
        " MAX(record_updated_on) AS record_updated_on"
        f" FROM {schema}.{ENROLLMENT_MV} WHERE {' AND '.join(scope)} GROUP BY user_pk"
    )
    # On the roster means mv_b2b_learner says roster or both. Enrolled means at
    # least one enrollment counted by courses_enrolled, as in the view, so a
    # roster member whose only enrollment is inactive reads as `roster` by default
    # and as `both` under include_inactive.
    records = (
        "SELECT COALESCE(l.user_pk, e.user_pk) AS user_pk,"  # noqa: S608
        " COALESCE(l.user_global_id, e.user_global_id) AS learner_id,"
        " COALESCE(l.email, e.email) AS email, COALESCE(l.full_name, e.full_name) AS full_name,"
        " COALESCE(l.sso_organization_id, e.sso_organization_id) AS organization_id,"
        " COALESCE(l.organization_name, e.organization_name) AS organization_name,"
        " CASE WHEN l.membership_source IN ('roster', 'both') AND e.courses_enrolled > 0"
        " THEN 'both' WHEN l.membership_source IN ('roster', 'both') THEN 'roster'"
        " ELSE 'enrollment' END AS membership_source,"
        " COALESCE(l.is_organization_manager, FALSE) AS is_organization_manager,"
        " e.first_enrolled_on, e.last_enrolled_on,"
        " COALESCE(e.courses_enrolled, 0) AS courses_enrolled,"
        " COALESCE(e.courses_passed, 0) AS courses_passed,"
        " COALESCE(e.courses_certified, 0) AS courses_certified,"
        f" COALESCE(e.courses_certified, 0) + {program_certificates} AS certificates_earned,"
        f"{_LEARNER_PENDING} {record_updated_on} AS record_updated_on"
        f" FROM (SELECT * FROM {schema}.{LEARNER_MV} WHERE sso_organization_id = %s) l"
        f" {join} ({enrollment_rollup}) e ON l.user_pk = e.user_pk"
    )
    return records, params
