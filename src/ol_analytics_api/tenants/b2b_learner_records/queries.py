"""SQL for the learner-records collections.

Every query is a fixed template. The identifiers are this module's constants
plus the tenant's validated schema name, and every caller-supplied value is a
bound parameter. The only per-request variation is which of a fixed set of
predicates is included, and code chooses that, not input. That is the
justification for each ``# noqa: S608`` below.

Each query is two layers. The inner ``records`` select renames MV columns to
the API's names and carries three internal columns: ``user_pk`` (a paging
tie-break, never returned), ``changed_on`` (the ``updated_since`` cursor) and
``removed``. The outer select applies consent, the cross-collection filters,
ordering and paging, so the page and its count always share one WHERE clause.

``changed_on`` comes from each MV's change log, ``<mv>_changes``, which
ol-data-platform's Dagster MV refresh rewrites after every refresh
(lakehouse.lib.starrocks_dbt.stamp_change_log). A log row's ``changed_on`` moves
whenever any column of its MV row changes, and is at or after the finish of the
refresh that exposed the change, so it is at or after every ``as_of`` a client
could hold from before it saw the change. The comparison against
``updated_since`` must stay inclusive for that to hold. A row the log has not
stamped yet (a refresh landed after the last stamp) has no ``changed_on``: a
full read returns it, and an incremental one picks it up after the stamp,
whose time is after that refresh and so after the ``as_of`` the client holds.

A record that has left its MV survives only in the log, with ``is_deleted`` set
and the identity columns it last had. Under ``updated_since`` these come back as
removed records: the identity fields and ``removed: true``, nothing else. A log
row whose identity a live row still holds is not a removal (a warehouse
surrogate re-keyed), so it is left out rather than sent beside the live record.

Consent is enforced here. ``_outcomes_shared()`` returns the SQL expression
deciding whether a record's outcomes may be disclosed. The MVs carry the
learner's recorded decision as a nullable ``outcomes_shared`` (null = none
recorded, ol-data-platform#2785), which the inner selects project as
``outcomes_decision``; the outer alias ``outcomes_shared`` is the resolved
answer, and a WHERE clause must never see that name mean both. The expression
is ``COALESCE(outcomes_decision, <literal>)``, the literal chosen by
``consent_fail_open``: a recorded decision always wins, and the setting only
covers learners with none (FALSE by default, which fails closed). Both it and
``outcomes_consent_status``, the unresolved decision, come from core/consent.py,
which the b2b_dashboard tenant shares.

Consent is recorded per contract. An enrollment carries its own contract's
decision. A learner rollup takes the decision for the contracts it counts
outcomes from: ``mv_b2b_learner``'s organization-grain one, or under
``contract_id`` that contract's alone (see ``_recomputed_learners``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from ol_analytics_api.core.consent import CONSENT_STATUS_SQL, outcomes_shared_sql
from ol_analytics_api.core.db.identifiers import validate_sql_identifier
from ol_analytics_api.tenants.b2b_learner_records.config import settings

LEARNER_MV = "mv_b2b_learner"
ENROLLMENT_MV = "mv_b2b_learner_enrollment"
CONTRACT_COURSERUN_MV = "mv_b2b_contract_courserun"
CHANGE_LOG_SUFFIX = "_changes"
LEARNER_CHANGES = f"{LEARNER_MV}{CHANGE_LOG_SUFFIX}"
ENROLLMENT_CHANGES = f"{ENROLLMENT_MV}{CHANGE_LOG_SUFFIX}"

# Each change log's key and identity, as ol-data-platform's meta.change_tracking
# on the MV declares them, then the stamp's own columns.
_LEARNER_KEY = ("organization_key", "user_pk")
_ENROLLMENT_KEY = ("contract_pk", "courserun_pk", "user_pk")
_STAMP_COLUMNS = ("row_hash", "changed_on", "is_deleted")
CHANGE_LOG_COLUMNS = {
    LEARNER_CHANGES: (*_LEARNER_KEY, "sso_organization_id", "user_global_id", *_STAMP_COLUMNS),
    ENROLLMENT_CHANGES: (
        *_ENROLLMENT_KEY,
        "sso_organization_id",
        "contract_id",
        "courserun_readable_id",
        "user_global_id",
        *_STAMP_COLUMNS,
    ),
}

# How long the API serves a removed record. An updated_since older than this is
# refused and the client reloads in full, so the change logs need keep removed
# rows only this long; the purge that will drop older ones is
# tk-purge-learner-records-change-log-tombstones-olde-76e06b in ol-data-platform.
REMOVED_RECORD_RETENTION = dt.timedelta(days=90)


def _outcomes_shared() -> str:
    return outcomes_shared_sql(fail_open=settings.consent_fail_open)


# Any recorded decline withholds, then any contract with no decision leaves it
# undecided, and only a consent on every contract shares.
_ROLLUP_DECISION = (
    "CASE WHEN SUM(CASE WHEN outcomes_shared = FALSE THEN 1 ELSE 0 END) > 0 THEN FALSE"
    " WHEN SUM(CASE WHEN outcomes_shared IS NULL THEN 1 ELSE 0 END) > 0 THEN NULL"
    " ELSE TRUE END"
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
    "content_progress",
    "certificate_issued_on",
    "certificate_is_revoked",
    "last_active_on",
    "days_active",
    "videos_watched",
    "problems_attempted",
    "chatbot_interactions",
)


@dataclass(frozen=True)
class RecordFilters:
    organization_id: uuid.UUID
    contract_id: int | None = None
    contract_is_active: bool | None = None
    courserun_id: str | None = None
    courserun_starts_after: dt.datetime | None = None
    courserun_starts_before: dt.datetime | None = None
    learner_ids: tuple[uuid.UUID, ...] = ()
    completion_statuses: tuple[str, ...] = ()
    updated_since: dt.datetime | None = None
    updated_before: dt.datetime | None = None
    include_inactive: bool = False


@dataclass(frozen=True)
class RecordQuery:
    """``params`` binds ``count``; ``page`` takes ``params`` plus LIMIT and OFFSET.

    ``sources`` names the MVs read, for the envelope's ``as_of``.
    ``change_logs`` names the change logs read, whose start bounds how far back
    ``updated_since`` can reach (see ``change_log_start``)."""

    page: str
    count: str
    params: tuple[Any, ...]
    sources: tuple[str, ...]
    change_logs: tuple[str, ...] = ()


def _placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


def _whole_second(value: dt.datetime, *, round_up: bool = False) -> dt.datetime:
    """A zone-less UTC bound, rounded to the whole second toward re-sending.

    The MVs store every MITx Online timestamp as a zone-less UTC ISO-8601
    string, compared lexicographically, and the change logs stamp
    ``changed_on`` from ``utc_timestamp()``; only whole seconds are compared
    safely against either. For an inclusive lower bound (the default, ``round_up=False``),
    truncating down makes the bound a prefix of any stored value in the same
    second, so a record at the boundary is matched by ``>=`` rather than
    skipped — the record may be re-sent by the next window too, which a
    sync client tolerates, but it is never dropped.

    ``round_up=True`` is the same trade for an exclusive upper bound: rounding
    a fractional instant *down* would turn ``< bound`` into ``< floor(bound)``,
    which excludes every record in that second, including ones genuinely
    before the requested instant — a silent drop, not a re-send. Rounding up
    to the next whole second instead means ``<`` matches everything through
    that second, so the same record may be re-sent by the window that starts
    there (its ``since`` floors to the same second), never lost between them.
    """
    if value.tzinfo is not None:
        value = value.astimezone(dt.UTC).replace(tzinfo=None)
    if round_up and value.microsecond:
        return value.replace(microsecond=0) + dt.timedelta(seconds=1)
    return value.replace(microsecond=0)


def _cursor_value(value: dt.datetime, *, round_up: bool = False) -> str:
    """``_whole_second`` as the ISO-8601 text an MV timestamp string compares with."""
    return _whole_second(value, round_up=round_up).isoformat()


def _assemble(  # noqa: PLR0913, PLR0917
    records: str,
    record_params: list[Any],
    columns: tuple[str, ...],
    outcomes: tuple[str, ...],
    order_by: tuple[str, ...],
    predicates: list[str],
    predicate_params: list[Any],
    sources: tuple[str, ...],
    change_logs: tuple[str, ...],
) -> RecordQuery:
    shared = _outcomes_shared()
    projection = ", ".join(
        [
            *columns,
            "removed",
            f"{CONSENT_STATUS_SQL} AS outcomes_consent_status",
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
        # A removed record has no outcomes to withhold.
        f" SUM(CASE WHEN removed OR {shared} THEN 0 ELSE 1 END) AS outcomes_withheld_count"
        f" FROM ({records}) records{where}"
    )
    return RecordQuery(page, count, (*record_params, *predicate_params), sources, change_logs)


def _window_predicates(
    column: str,
    since: dt.datetime | None,
    before: dt.datetime | None,
    *,
    string_column: bool = False,
) -> tuple[list[str], list[Any]]:
    """``[since, before)``: paired with a later window whose ``since`` is this
    ``before``, a backfill can hand each window to one worker and know no row
    is dropped between them. Both bounds round toward re-sending a row rather
    than skipping it (see ``_whole_second``), so a row at a sub-second boundary
    can land in both windows, never in neither. A ``string_column`` compares as
    ISO-8601 text, the change logs' ``changed_on`` as a DATETIME.
    ``column`` is always one of this module's own fixed identifiers, never
    caller input.
    """
    predicates: list[str] = []
    params: list[Any] = []
    if since is not None:
        predicates.append(f"{column} >= %s")
        params.append(_cursor_value(since) if string_column else _whole_second(since))
    if before is not None:
        predicates.append(f"{column} < %s")
        params.append(
            _cursor_value(before, round_up=True)
            if string_column
            else _whole_second(before, round_up=True)
        )
    return predicates, params


def _shared_predicates(filters: RecordFilters) -> tuple[list[str], list[Any]]:
    predicates: list[str] = []
    params: list[Any] = []
    if filters.learner_ids:
        predicates.append(f"learner_id IN ({_placeholders(len(filters.learner_ids))})")
        params.extend(str(learner_id) for learner_id in filters.learner_ids)
    window_predicates, window_params = _window_predicates(
        "changed_on", filters.updated_since, filters.updated_before
    )
    predicates.extend(window_predicates)
    params.extend(window_params)
    return predicates, params


def _with_removed(
    live: str, removed: str, columns: tuple[str, ...], identity: tuple[str, ...]
) -> str:
    """The live records followed by the removed ones, in ``columns`` order.

    ``removed`` selects ``identity`` and ``changed_on``; every other column is
    NULL there. Both halves are selected by name so the UNION's positional
    matching can't pair columns wrongly.
    """
    removed_columns = {*identity, "changed_on"}
    removed_projection = ", ".join(
        "TRUE AS removed"
        if name == "removed"
        else (name if name in removed_columns else f"NULL AS {name}")
        for name in columns
    )
    return (
        f"SELECT {', '.join(columns)} FROM ({live}) live"  # noqa: S608
        f" UNION ALL SELECT {removed_projection} FROM ({removed}) gone"
    )


def _change_log_join(log: str, key: tuple[str, ...]) -> str:
    """LEFT JOIN a change log on ``key`` for its ``changed_on``.

    The log's key columns are renamed so the MV's own names stay unambiguous.
    The comparison is null-safe, as the stamp's own join is, so a null key
    column still finds its log row.
    """
    renamed = ", ".join(f"{name} AS log_{name}" for name in key)
    on = " AND ".join(f"{name} <=> log_{name}" for name in key)
    return f" LEFT JOIN (SELECT {renamed}, changed_on FROM {log}) log ON {on}"  # noqa: S608


_ENROLLMENT_RECORD = (
    "user_pk",
    *_ENROLLMENT_COLUMNS,
    *_ENROLLMENT_OUTCOMES,
    "outcomes_decision",
    "changed_on",
    "removed",
)
_LEARNER_RECORD = (
    "user_pk",
    *_LEARNER_COLUMNS,
    *_LEARNER_OUTCOMES,
    "outcomes_decision",
    "changed_on",
    "removed",
)


def enrollments(schema: str, filters: RecordFilters) -> RecordQuery:
    schema = validate_sql_identifier(schema)
    identity = ["sso_organization_id = %s"]
    identity_params: list[Any] = [str(filters.organization_id)]
    if filters.contract_id is not None:
        identity.append("contract_id = %s")
        identity_params.append(filters.contract_id)
    if filters.courserun_id is not None:
        identity.append("courserun_readable_id = %s")
        identity_params.append(filters.courserun_id)
    scope = list(identity)
    if not filters.include_inactive:
        scope.append("enrollment_is_active = TRUE")
    records = (
        "SELECT user_pk, user_global_id AS learner_id, email, full_name,"  # noqa: S608
        " sso_organization_id AS organization_id, contract_id,"
        " b2b_contract_name AS contract_name, courserun_readable_id AS courserun_id,"
        " courserun_title, courserun_start_on, courserun_end_on,"
        " enrollment_created_on AS enrolled_on, enrollment_is_active, enrollment_mode,"
        " enrollment_status, completion_status, is_passing,"
        " grade_value AS grade, letter_grade, content_progress, certificate_issued_on,"
        " certificate_is_revoked,"
        " last_active_on, days_active, videos_played AS videos_watched, problems_attempted,"
        " chatbot_interactions, outcomes_shared AS outcomes_decision, changed_on,"
        " FALSE AS removed"
        f" FROM {schema}.{ENROLLMENT_MV}"
        f"{_change_log_join(f'{schema}.{ENROLLMENT_CHANGES}', _ENROLLMENT_KEY)}"
        f" WHERE {' AND '.join(scope)}"
    )
    record_params = list(identity_params)

    # A status filter can't match a removed record, which has no status, so a
    # filtered sync gets no removals; the contract says so.
    if filters.updated_since is not None and not filters.completion_statuses:
        # A re-key leaves a tombstone on the old key, and deleting the new key
        # leaves another, so one identity can hold several. Its removal is the
        # latest of them.
        removed = (
            "SELECT MAX(c.user_pk) AS user_pk, c.user_global_id AS learner_id,"  # noqa: S608
            " c.sso_organization_id AS organization_id, c.contract_id,"
            " c.courserun_readable_id AS courserun_id, MAX(c.changed_on) AS changed_on"
            f" FROM {schema}.{ENROLLMENT_CHANGES} c"
            f" WHERE c.is_deleted = TRUE AND {' AND '.join(f'c.{p}' for p in identity)}"
            f" AND NOT EXISTS (SELECT 1 FROM {schema}.{ENROLLMENT_MV} m"
            " WHERE m.sso_organization_id = c.sso_organization_id"
            " AND m.user_global_id = c.user_global_id AND m.contract_id = c.contract_id"
            " AND m.courserun_readable_id = c.courserun_readable_id)"
            " GROUP BY c.user_global_id, c.sso_organization_id, c.contract_id,"
            " c.courserun_readable_id"
        )
        records = _with_removed(
            records,
            removed,
            _ENROLLMENT_RECORD,
            ("user_pk", "learner_id", "organization_id", "contract_id", "courserun_id"),
        )
        record_params.extend(identity_params)

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
        record_params,
        _ENROLLMENT_COLUMNS,
        _ENROLLMENT_OUTCOMES,
        ("learner_id", "contract_id", "courserun_id", "user_pk"),
        predicates,
        params,
        (ENROLLMENT_MV,),
        (ENROLLMENT_CHANGES,),
    )


def learners(schema: str, filters: RecordFilters) -> RecordQuery:
    """The learner rollup.

    ``mv_b2b_learner`` precomputes it across all the organization's contracts,
    which serves the default request. ``courses_enrolled``, the enrolled_on
    dates, ``both`` membership, ``last_active_on`` and ``courses_in_progress``
    count active enrollments. Completions and the
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
            " courses_passed, courses_certified, certificates_earned,"
            " outcomes_shared AS outcomes_decision, outcomes_consent_on,"
            " last_active_on, courses_in_progress, changed_on, FALSE AS removed"
            f" FROM {schema}.{LEARNER_MV}"
            f"{_change_log_join(f'{schema}.{LEARNER_CHANGES}', _LEARNER_KEY)}"
            " WHERE sso_organization_id = %s"
        )
        record_params: list[Any] = [str(filters.organization_id)]
        sources: tuple[str, ...] = (LEARNER_MV,)
        change_logs: tuple[str, ...] = (LEARNER_CHANGES,)
    else:
        records, record_params = _recomputed_learners(schema, filters)
        sources = (LEARNER_MV, ENROLLMENT_MV)
        change_logs = (LEARNER_CHANGES, ENROLLMENT_CHANGES)

    if filters.updated_since is not None:
        removed, removed_params = _removed_learners(schema, filters)
        records = _with_removed(
            records, removed, _LEARNER_RECORD, ("user_pk", "learner_id", "organization_id")
        )
        record_params.extend(removed_params)

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
        change_logs,
    )


def _recomputed_learners(schema: str, filters: RecordFilters) -> tuple[str, list[Any]]:
    organization = str(filters.organization_id)
    scope = ["sso_organization_id = %s"]
    scope_params: list[Any] = [organization]
    if filters.contract_id is not None:
        scope.append("contract_id = %s")
        scope_params.append(filters.contract_id)

    # The same counts as mv_b2b_learner, over the same flags: both views take
    # is_passing, is_certified and is_in_progress from ol-data-platform's
    # afact_learner_courserun_progress, so no business rule is restated here.
    # courses_enrolled, the enrolled_on dates, last_active_on and
    # courses_in_progress look at enrollment_is_active, and only without
    # include_inactive: they describe current engagement. Completions span every
    # enrollment, so a filtered request can't report fewer completions than the
    # default one.
    in_progress = "is_in_progress = TRUE"
    if filters.include_inactive:
        enrolled_run = "courserun_pk"
        enrolled_on = "enrollment_created_on"
        active_on = "last_active_on"
    else:
        enrolled_run = "CASE WHEN enrollment_is_active = TRUE THEN courserun_pk END"
        enrolled_on = "CASE WHEN enrollment_is_active = TRUE THEN enrollment_created_on END"
        active_on = "CASE WHEN enrollment_is_active = TRUE THEN last_active_on END"
        in_progress = f"enrollment_is_active = TRUE AND {in_progress}"

    # certificates_earned is mv_b2b_learner's courses_certified plus
    # program_certificates_earned. This rollup recounts the course certificates
    # from e, so it adds the program certificates to that count itself rather
    # than reading the view's sum, which could disagree with e's
    # courses_certified while the two MVs refresh.
    if filters.contract_id is not None:
        # Learners with any enrollment in the contract, active or not, as the
        # default rollup keeps learners whose seats were reclaimed. A program
        # certificate has no course run and so belongs to no one contract; it is
        # left out, matching "every rollup from that contract's enrollments only".
        join = "RIGHT JOIN"
        program_certificates = "0"
        # Outcomes come from this contract alone, so its decision is the one
        # that applies, as on the contract's enrollment records.
        decision = "e.outcomes_decision"
        consent_on = "CASE WHEN e.outcomes_decision THEN e.outcomes_consent_on END"
    else:
        # include_inactive: every roster member, plus anyone enrolled at all.
        join = "FULL OUTER JOIN"
        program_certificates = "COALESCE(l.program_certificates_earned, 0)"
        # mv_b2b_learner resolves every contract of the organization the learner
        # has a membership or any enrollment under, which is this rollup's scope.
        # The two MVs refresh one after the other, so either can be ahead of the
        # other. Where both have the learner the more conservative answer wins
        # (a decline, then no decision), as the counts here come from e.
        decision = (
            "CASE WHEN e.outcomes_decision = FALSE OR l.outcomes_shared = FALSE THEN FALSE"
            " WHEN l.user_pk IS NULL THEN e.outcomes_decision"
            " WHEN e.user_pk IS NULL THEN l.outcomes_shared"
            " WHEN e.outcomes_decision IS NULL THEN NULL ELSE l.outcomes_shared END"
        )
        consent_on = (
            f"CASE WHEN {decision} THEN CASE WHEN l.user_pk IS NOT NULL"
            " THEN l.outcomes_consent_on ELSE e.outcomes_consent_on END END"
        )

    enrollment_rollup = (
        "SELECT user_pk, MAX(user_global_id) AS user_global_id, MAX(email) AS email,"  # noqa: S608
        " MAX(full_name) AS full_name,"
        " MAX(sso_organization_id) AS sso_organization_id,"
        " MAX(organization_name) AS organization_name,"
        f" MIN({enrolled_on}) AS first_enrolled_on,"
        f" MAX({enrolled_on}) AS last_enrolled_on,"
        f" COUNT(DISTINCT {enrolled_run}) AS courses_enrolled,"
        " COUNT(DISTINCT CASE WHEN is_passing = TRUE THEN courserun_pk END) AS courses_passed,"
        " COUNT(DISTINCT CASE WHEN is_certified = TRUE THEN courserun_pk END)"
        " AS courses_certified,"
        f" MAX({active_on}) AS last_active_on,"
        f" COUNT(DISTINCT CASE WHEN {in_progress} THEN courserun_pk END) AS courses_in_progress,"
        f" {_ROLLUP_DECISION} AS outcomes_decision,"
        " MAX(outcomes_consent_on) AS outcomes_consent_on"
        f" FROM {schema}.{ENROLLMENT_MV} WHERE {' AND '.join(scope)} GROUP BY user_pk"
    )
    # On the roster means mv_b2b_learner says roster or both. Enrolled means at
    # least one enrollment counted by courses_enrolled, as in the view, so a
    # roster member whose only enrollment is inactive reads as `roster` by default
    # and as `both` under include_inactive.
    # last_active_on comes from e alone. mv_b2b_learner rolls it up from the same
    # enrollments as mv_b2b_learner_enrollment, active ones only, so under
    # include_inactive e's value covers l's, and a learner with no enrollment is
    # null in both. Under contract_id, l's value spans other contracts.
    records = (
        "SELECT COALESCE(l.user_pk, e.user_pk) AS user_pk,"  # noqa: S608
        " COALESCE(l.user_global_id, e.user_global_id) AS learner_id,"
        " COALESCE(l.email, e.email) AS email,"
        " COALESCE(l.full_name, e.full_name) AS full_name,"
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
        f" {decision} AS outcomes_decision, {consent_on} AS outcomes_consent_on,"
        " e.last_active_on,"
        " COALESCE(e.courses_in_progress, 0) AS courses_in_progress"
        f" FROM (SELECT * FROM {schema}.{LEARNER_MV} WHERE sso_organization_id = %s) l"
        f" {join} ({enrollment_rollup}) e ON l.user_pk = e.user_pk"
    )
    # The cursor is the latest change to anything the record is built from: the
    # learner's mv_b2b_learner row (identity, roster, manager flag) and every
    # enrollment the rollup reads, deleted ones included, since a deleted or
    # deactivated enrollment changes the counts. Under contract_id the learner
    # row also changes with other contracts, which only re-sends the record.
    learner_log = (
        "SELECT user_pk AS log_user_pk, MAX(changed_on) AS learner_changed_on"  # noqa: S608
        f" FROM {schema}.{LEARNER_CHANGES} WHERE sso_organization_id = %s GROUP BY user_pk"
    )
    enrollment_log = (
        "SELECT user_pk AS log_user_pk, MAX(changed_on) AS enrollment_changed_on"  # noqa: S608
        f" FROM {schema}.{ENROLLMENT_CHANGES} WHERE {' AND '.join(scope)} GROUP BY user_pk"
    )
    # l, then e, then the learner log, then the enrollment log.
    params = [organization, *scope_params, organization, *scope_params]
    rollup_columns = ", ".join(
        f"r.{name}" for name in _LEARNER_RECORD if name not in {"changed_on", "removed"}
    )
    records = (
        f"SELECT {rollup_columns},"  # noqa: S608
        " CASE WHEN ll.learner_changed_on IS NULL THEN el.enrollment_changed_on"
        " WHEN el.enrollment_changed_on IS NULL THEN ll.learner_changed_on"
        " ELSE GREATEST(ll.learner_changed_on, el.enrollment_changed_on) END AS changed_on,"
        f" FALSE AS removed FROM ({records}) r"
        f" LEFT JOIN ({learner_log}) ll ON r.user_pk = ll.log_user_pk"
        f" LEFT JOIN ({enrollment_log}) el ON r.user_pk = el.log_user_pk"
    )
    return records, params


def _removed_learners(schema: str, filters: RecordFilters) -> tuple[str, list[Any]]:
    """Learners this request's rollup once held and holds no longer.

    The rollup holds the roster (``mv_b2b_learner``) by default, under
    ``contract_id`` the learners with an enrollment in that contract, and under
    ``include_inactive`` the roster plus anyone enrolled. A learner is removed
    once every change-log row for those sources is deleted and no live row in
    them carries the learner's identity.
    """
    views: tuple[str, ...]
    if filters.contract_id is not None:
        views = (ENROLLMENT_MV,)
        in_contract = " AND {alias}contract_id = %s"
        contract_params: list[Any] = [filters.contract_id]
    else:
        views = (LEARNER_MV, ENROLLMENT_MV) if filters.include_inactive else (LEARNER_MV,)
        in_contract = ""
        contract_params = []

    logs = " UNION ALL ".join(
        "SELECT user_pk, user_global_id, sso_organization_id, changed_on, is_deleted"  # noqa: S608
        f" FROM {schema}.{view}{CHANGE_LOG_SUFFIX}"
        f" WHERE sso_organization_id = %s{in_contract.format(alias='')}"
        for view in views
    )
    still_held = "".join(
        f" AND NOT EXISTS (SELECT 1 FROM {schema}.{view} m"  # noqa: S608
        " WHERE m.sso_organization_id = g.organization_id"
        f" AND m.user_global_id = g.learner_id{in_contract.format(alias='m.')})"
        for view in views
    )
    # Grouped by identity, not user_pk: a re-keyed learner who is then removed
    # leaves tombstones under both keys and is one removal.
    removed = (
        "SELECT user_pk, learner_id, organization_id, changed_on"  # noqa: S608
        " FROM (SELECT MAX(user_pk) AS user_pk, user_global_id AS learner_id,"
        " MAX(sso_organization_id) AS organization_id, MAX(changed_on) AS changed_on"
        f" FROM ({logs}) c GROUP BY user_global_id"
        " HAVING MIN(CASE WHEN is_deleted THEN 1 ELSE 0 END) = 1) g"
        f" WHERE TRUE{still_held}"
    )
    log_params = [str(filters.organization_id), *contract_params] * len(views)
    return removed, [*log_params, *contract_params * len(views)]


def change_log_start(schema: str, log: str) -> str:
    """The earliest ``changed_on`` in *log*.

    The seed stamps every row with the log's creation time and a stamp only
    moves ``changed_on`` forward, so this is at or after the creation. A record
    removed before the log existed has no row in it, so an ``updated_since``
    earlier than this could miss that removal. Deliberately conservative: once
    every seeded row has changed, it is later than the creation and refuses a
    cursor that was safe, which costs that client a full reload, not a removal.
    """
    return (
        "SELECT MIN(changed_on) AS started"  # noqa: S608
        f" FROM {validate_sql_identifier(schema)}.{validate_sql_identifier(log)}"
    )


def courses(schema: str, filters: RecordFilters) -> RecordQuery:
    """The organization's contracts and their course runs.

    Built without ``_assemble``: these rows carry no personal data, so there is
    no consent projection, and ``outcomes_withheld_count`` is always 0.

    This MV carries no ``record_updated_on``, so ``courserun_starts_after``/
    ``courserun_starts_before`` partition by when a run starts, not by when its
    row last changed. ``courserun_start_on`` is nullable (unscheduled runs), and
    SQL comparisons against NULL are never true, so a self-paced run with no
    start date matches neither bound and falls outside every partition. A
    partitioned backfill that wants full coverage still needs one unfiltered
    pass (or a pass with neither bound set) to pick those up.
    """
    table = f"{validate_sql_identifier(schema)}.{CONTRACT_COURSERUN_MV}"
    scope = ["sso_organization_id = %s"]
    params: list[Any] = [str(filters.organization_id)]
    if filters.contract_id is not None:
        scope.append("contract_id = %s")
        params.append(filters.contract_id)
    if filters.contract_is_active is not None:
        scope.append("b2b_contract_is_active = %s")
        params.append(filters.contract_is_active)
    if filters.courserun_id is not None:
        scope.append("courserun_readable_id = %s")
        params.append(filters.courserun_id)
    window_scope, window_params = _window_predicates(
        "courserun_start_on",
        filters.courserun_starts_after,
        filters.courserun_starts_before,
        string_column=True,
    )
    scope.extend(window_scope)
    params.extend(window_params)
    where = " AND ".join(scope)
    page = (
        "SELECT sso_organization_id AS organization_id, organization_name, contract_id,"  # noqa: S608
        " b2b_contract_name AS contract_name, b2b_contract_is_active AS contract_is_active,"
        " b2b_contract_start_date AS contract_start_date,"
        " b2b_contract_end_date AS contract_end_date, seat_limit,"
        " courserun_readable_id AS courserun_id, courserun_title, courserun_start_on,"
        f" courserun_end_on FROM {table} WHERE {where}"
        " ORDER BY contract_id, courserun_id LIMIT %s OFFSET %s"
    )
    count = (
        "SELECT COUNT(*) AS total_count, 0 AS outcomes_withheld_count"  # noqa: S608
        f" FROM {table} WHERE {where}"
    )
    return RecordQuery(page, count, tuple(params), (CONTRACT_COURSERUN_MV,))
