"""SQL for the contract-scoped learner-progress endpoint.

These rows identify individual learners, so they do not go through
core/db/query.py's anonymization chokepoint: a k-anonymity floor would
suppress every one of them. Org-manager authorization and the contract gate
still apply (routers/learners.py).

Every query is a fixed template. The identifiers are this module's constants
plus the validated schema name, and every caller-supplied value is a bound
parameter. The only per-request variation is which of a fixed set of
predicates and orderings is included, and code chooses that, not input. That
is the justification for each ``# noqa: S608`` below.

Each query is two layers, as in the b2b_learner_records tenant. The inner
``records`` select scopes to the organization and contract and derives
``completion_status``; the outer select applies consent, search and the status
filter, so the page and its count always share one WHERE clause.

Consent gates outcome fields, not the row. No consent field exists upstream
yet, so ``consent_fail_open`` picks the literal that decides every row. When
the field lands, ``_outcomes_shared`` becomes ``COALESCE(<field>, <literal>)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ol_analytics_api.core.db.identifiers import validate_sql_identifier
from ol_analytics_api.tenants.b2b_dashboard.config import settings

ENROLLMENT_MV = "mv_b2b_learner_enrollment"

# Matches the b2b_learner_records tenant. An unrevoked certificate is certified
# without requiring is_passing, since production has unrevoked certificates with
# is_passing false (ol-data-platform#2669). Until activity data lands,
# "in progress" can only mean a nonzero grade.
_COMPLETION_STATUS = (
    "CASE"
    " WHEN certificate_is_revoked = FALSE THEN 'certified'"
    " WHEN is_passing = TRUE THEN 'passed'"
    " WHEN grade_value > 0 THEN 'in_progress'"
    " ELSE 'not_started'"
    " END"
)

# Upstream stores "" rather than NULL for learners who never set a name. Null
# blank names so they sort with the missing ones instead of before every name.
_BLANK_AS_NULL_NAME = "NULLIF(TRIM(full_name), '')"

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


def learner_progress(filters: ProgressFilters) -> ProgressQuery:
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
        " letter_grade, certificate_issued_on, certificate_is_revoked"
        f" FROM {table} WHERE {' AND '.join(scope)}"
    )

    shared = _outcomes_shared()
    predicates: list[str] = []
    if filters.search:
        pattern = _contains_pattern(filters.search)
        predicates.append("(LOWER(email) LIKE %s OR LOWER(full_name) LIKE %s)")
        params.extend([pattern, pattern])
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
            "NULL AS last_active_on",
        ]
    )
    page = (
        f"SELECT {projection} FROM ({records}) records{where}"  # noqa: S608
        f" ORDER BY {order_by} LIMIT %s OFFSET %s"
    )
    count = (
        "SELECT COUNT(*) AS total_count,"  # noqa: S608
        f" SUM(CASE WHEN {shared} THEN 0 ELSE 1 END) AS outcomes_withheld_count"
        f" FROM ({records}) records{where}"
    )
    return ProgressQuery(page, count, tuple(params))
