"""Consent resolution in the b2b_learner_records queries, run against sqlite.

test_learner_records.py pins the SQL text. These tests execute it, because the
rule that matters is about rows: a recorded decision wins over
``consent_fail_open``, and the setting only covers learners with none.
"""

import sqlite3
import uuid

import pytest

from ol_analytics_api.tenants.b2b_learner_records import queries
from ol_analytics_api.tenants.b2b_learner_records.config import settings

SCHEMA = "b2b_learner_records"
ORG_ID = "8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21"
CONSENT_ON = "2026-02-03T14:20:04.000000"

_ENROLLMENT_MV_COLUMNS = (
    "user_pk",
    "user_global_id",
    "email",
    "full_name",
    "sso_organization_id",
    "organization_name",
    "contract_id",
    "b2b_contract_name",
    "courserun_pk",
    "courserun_readable_id",
    "courserun_title",
    "courserun_start_on",
    "courserun_end_on",
    "enrollment_created_on",
    "enrollment_is_active",
    "enrollment_mode",
    "enrollment_status",
    "is_passing",
    "grade_value",
    "letter_grade",
    "certificate_issued_on",
    "certificate_is_revoked",
    "last_active_on",
    "days_active",
    "videos_played",
    "problems_attempted",
    "chatbot_interactions",
    "outcomes_shared",
    "outcomes_consent_on",
    "record_updated_on",
)
_LEARNER_MV_COLUMNS = (
    "user_pk",
    "user_global_id",
    "email",
    "full_name",
    "sso_organization_id",
    "organization_name",
    "membership_source",
    "is_organization_manager",
    "first_enrolled_on",
    "last_enrolled_on",
    "courses_enrolled",
    "courses_passed",
    "courses_certified",
    "program_certificates_earned",
    "last_active_on",
    "courses_in_progress",
    "outcomes_shared",
    "outcomes_consent_on",
    "record_updated_on",
)


def _insert(conn, mv, columns, **values):
    row = [values.get(name) for name in columns]
    conn.execute(
        f"INSERT INTO {SCHEMA}.{mv} VALUES ({', '.join('?' * len(row))})",  # noqa: S608
        row,
    )


def _enroll(conn, name, contract_id, *, decision, consent_on=None):
    _insert(
        conn,
        queries.ENROLLMENT_MV,
        _ENROLLMENT_MV_COLUMNS,
        user_pk=name,
        user_global_id=name,
        sso_organization_id=ORG_ID,
        contract_id=contract_id,
        courserun_pk=f"run-{contract_id}",
        courserun_readable_id=f"run-{contract_id}",
        enrollment_is_active=True,
        is_passing=True,
        grade_value=0.8,
        outcomes_shared=decision,
        outcomes_consent_on=consent_on,
    )


def _roster(conn, name, *, decision, consent_on=None):
    _insert(
        conn,
        queries.LEARNER_MV,
        _LEARNER_MV_COLUMNS,
        user_pk=name,
        user_global_id=name,
        sso_organization_id=ORG_ID,
        membership_source="both",
        is_organization_manager=False,
        courses_enrolled=1,
        courses_passed=1,
        courses_certified=0,
        program_certificates_earned=0,
        courses_in_progress=0,
        outcomes_shared=decision,
        outcomes_consent_on=consent_on,
    )


@pytest.fixture
def conn():
    """Both learner MVs. Every learner passes each run they are enrolled in.

    Under contract 42, ``all_in`` said yes, ``declined`` said no and
    ``undecided`` has no decision. ``consented`` and ``mixed`` said yes under
    42 and also hold a run under 43, where ``consented`` has no decision and
    ``mixed`` said no. The ``mv_b2b_learner`` rows carry the decision that
    view would resolve across both contracts.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("GREATEST", 2, max)
    conn.execute(f"ATTACH ':memory:' AS {SCHEMA}")
    conn.execute(
        f"CREATE TABLE {SCHEMA}.{queries.ENROLLMENT_MV} ({', '.join(_ENROLLMENT_MV_COLUMNS)})"
    )
    conn.execute(f"CREATE TABLE {SCHEMA}.{queries.LEARNER_MV} ({', '.join(_LEARNER_MV_COLUMNS)})")

    _enroll(conn, "all_in", 42, decision=True, consent_on=CONSENT_ON)
    _enroll(conn, "declined", 42, decision=False)
    _enroll(conn, "undecided", 42, decision=None)
    _enroll(conn, "consented", 42, decision=True, consent_on=CONSENT_ON)
    _enroll(conn, "consented", 43, decision=None)
    _enroll(conn, "mixed", 42, decision=True, consent_on=CONSENT_ON)
    _enroll(conn, "mixed", 43, decision=False)
    _roster(conn, "all_in", decision=True, consent_on=CONSENT_ON)
    _roster(conn, "declined", decision=False)
    _roster(conn, "undecided", decision=None)
    _roster(conn, "consented", decision=None)
    _roster(conn, "mixed", decision=False)
    yield conn
    conn.close()


def _filters(**overrides):
    return queries.RecordFilters(organization_id=uuid.UUID(ORG_ID), **overrides)


def _enrollments(conn, **filters):
    query = queries.enrollments(SCHEMA, _filters(**filters))
    rows = conn.execute(query.page.replace("%s", "?"), (*query.params, 100, 0)).fetchall()
    count = conn.execute(query.count.replace("%s", "?"), query.params).fetchone()
    by_key = {(row["learner_id"], row["contract_id"]): row for row in rows}
    return by_key, count["outcomes_withheld_count"]


def _learners(conn, **filters):
    query = queries.learners(SCHEMA, _filters(**filters))
    rows = conn.execute(query.page.replace("%s", "?"), (*query.params, 100, 0)).fetchall()
    count = conn.execute(query.count.replace("%s", "?"), query.params).fetchone()
    return {row["learner_id"]: row for row in rows}, count["outcomes_withheld_count"]


def _disclosed(rows, field):
    return {key for key, row in rows.items() if row[field] is not None}


def test_enrollments_disclose_only_recorded_consent_when_failing_closed(conn):
    assert settings.consent_fail_open is False
    rows, withheld = _enrollments(conn)
    assert len(rows) == 7
    assert _disclosed(rows, "grade") == {("all_in", 42), ("consented", 42), ("mixed", 42)}
    assert withheld == 4


def test_enrollments_withhold_a_recorded_decline_when_failing_open(conn, monkeypatch):
    monkeypatch.setattr(settings, "consent_fail_open", True)
    rows, withheld = _enrollments(conn)
    declines = {("declined", 42), ("mixed", 43)}
    assert {key for key, row in rows.items() if not row["outcomes_shared"]} == declines
    assert _disclosed(rows, "grade") == set(rows) - declines
    assert withheld == 2


def test_status_filter_follows_the_recorded_decision(conn, monkeypatch):
    monkeypatch.setattr(settings, "consent_fail_open", True)
    # The decliners passed too, and `passed` must not say so.
    passed, _ = _enrollments(conn, completion_statuses=("passed",))
    assert not {("declined", 42), ("mixed", 43)} & set(passed)
    unknown, _ = _enrollments(conn, completion_statuses=("unknown",))
    assert set(unknown) == {("declined", 42), ("mixed", 43)}


@pytest.mark.parametrize("fail_open", [False, True])
def test_consent_status_reports_the_recorded_decision_whatever_the_default(
    conn, monkeypatch, fail_open
):
    # outcomes_shared folds "no decision" into the deployment's default.
    # outcomes_consent_status doesn't, so an organization can tell a learner
    # who hasn't answered from one who declined.
    monkeypatch.setattr(settings, "consent_fail_open", fail_open)

    def statuses(rows):
        return {key: row["outcomes_consent_status"] for key, row in rows.items()}

    assert statuses(_enrollments(conn)[0]) == {
        ("all_in", 42): "consented",
        ("declined", 42): "declined",
        ("undecided", 42): "not_recorded",
        ("consented", 42): "consented",
        ("consented", 43): "not_recorded",
        ("mixed", 42): "consented",
        ("mixed", 43): "declined",
    }
    organization_grain = {
        "all_in": "consented",
        "declined": "declined",
        "undecided": "not_recorded",
        "consented": "not_recorded",
        "mixed": "declined",
    }
    assert statuses(_learners(conn)[0]) == organization_grain
    assert statuses(_learners(conn, include_inactive=True)[0]) == organization_grain
    assert statuses(_learners(conn, contract_id=42)[0]) == {
        **organization_grain,
        "consented": "consented",
        "mixed": "consented",
    }


@pytest.mark.parametrize("include_inactive", [False, True])
def test_learners_take_the_organization_decision(conn, monkeypatch, include_inactive):
    # The precomputed rollup and the include_inactive recompute both span every
    # contract, so they take mv_b2b_learner's decision, not one contract's:
    # `consented` and `mixed` said yes under 42 and are still not shared.
    rows, withheld = _learners(conn, include_inactive=include_inactive)
    assert _disclosed(rows, "courses_passed") == {"all_in"}
    assert rows["all_in"]["outcomes_consent_on"] == CONSENT_ON
    assert withheld == 4

    monkeypatch.setattr(settings, "consent_fail_open", True)
    rows, withheld = _learners(conn, include_inactive=include_inactive)
    assert _disclosed(rows, "courses_passed") == {"all_in", "consented", "undecided"}
    # Shared by the deployment's default, not by a recorded consent.
    assert rows["consented"]["outcomes_consent_on"] is None
    assert withheld == 2


def test_contract_scoped_learners_take_that_contracts_decision(conn, monkeypatch):
    assert settings.consent_fail_open is False
    rows, withheld = _learners(conn, contract_id=42)
    # 43 is out of scope, so what `consented` and `mixed` left there doesn't apply.
    assert _disclosed(rows, "courses_passed") == {"all_in", "consented", "mixed"}
    assert rows["mixed"]["outcomes_consent_on"] == CONSENT_ON
    assert withheld == 2

    rows, withheld = _learners(conn, contract_id=43)
    assert set(rows) == {"consented", "mixed"}
    assert _disclosed(rows, "courses_passed") == set()
    assert _disclosed(rows, "outcomes_consent_on") == set()
    assert withheld == 2

    monkeypatch.setattr(settings, "consent_fail_open", True)
    rows, withheld = _learners(conn, contract_id=42)
    assert _disclosed(rows, "courses_passed") == set(rows) - {"declined"}
    assert withheld == 1
    rows, withheld = _learners(conn, contract_id=43)
    assert _disclosed(rows, "courses_passed") == {"consented"}
    assert withheld == 1


def test_recomputed_rollup_resolves_learners_missing_from_the_learner_view(conn, monkeypatch):
    # With no mv_b2b_learner row the decision comes from the enrollments, by
    # that view's rule: any decline withholds, then any contract with no
    # decision leaves it undecided, and only a consent on every one shares.
    for name, other_contract in (("yes_no", False), ("yes_unasked", None), ("yes_yes", True)):
        _enroll(conn, name, 42, decision=True, consent_on=CONSENT_ON)
        _enroll(
            conn,
            name,
            43,
            decision=other_contract,
            consent_on=CONSENT_ON if other_contract else None,
        )
    added = {"yes_no", "yes_unasked", "yes_yes"}

    rows, _ = _learners(conn, include_inactive=True)
    assert _disclosed(rows, "courses_passed") & added == {"yes_yes"}
    assert _disclosed(rows, "outcomes_consent_on") & added == {"yes_yes"}

    monkeypatch.setattr(settings, "consent_fail_open", True)
    rows, _ = _learners(conn, include_inactive=True)
    assert _disclosed(rows, "courses_passed") & added == {"yes_unasked", "yes_yes"}
    assert _disclosed(rows, "outcomes_consent_on") & added == {"yes_yes"}


def test_a_decline_the_learner_view_has_not_caught_up_with_still_withholds(conn):
    # The two MVs refresh one after the other. The include_inactive counts come
    # from the enrollment view, so its decline wins over a stale consent.
    _enroll(conn, "stale", 42, decision=False)
    _roster(conn, "stale", decision=True, consent_on=CONSENT_ON)
    rows, _ = _learners(conn, include_inactive=True)
    assert rows["stale"]["outcomes_shared"] == 0
    assert rows["stale"]["courses_passed"] is None
    assert rows["stale"]["outcomes_consent_on"] is None


def test_a_missing_decision_the_learner_view_has_not_caught_up_with_is_undecided(conn, monkeypatch):
    # The same skew with no decision in the enrollment view (e.g. a contract
    # added since the learner view refreshed): the stale consent doesn't share.
    _enroll(conn, "stale", 42, decision=None)
    _roster(conn, "stale", decision=True, consent_on=CONSENT_ON)
    rows, _ = _learners(conn, include_inactive=True)
    assert rows["stale"]["outcomes_shared"] == 0
    assert rows["stale"]["courses_passed"] is None
    assert rows["stale"]["outcomes_consent_on"] is None

    monkeypatch.setattr(settings, "consent_fail_open", True)
    rows, _ = _learners(conn, include_inactive=True)
    assert rows["stale"]["courses_passed"] == 1
    # Shared by the deployment's default, so there is no consent date to report.
    assert rows["stale"]["outcomes_consent_on"] is None
