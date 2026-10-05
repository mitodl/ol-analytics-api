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


@pytest.fixture
def conn():
    """Both learner MVs, holding three learners who each pass one run under
    contract 42: ``consented`` said yes there, ``declined`` said no and
    ``undecided`` has no decision. ``consented`` also passed a run under
    contract 43 with no decision, so their organization-grain decision in
    ``mv_b2b_learner`` is null.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("GREATEST", 2, max)
    conn.execute(f"ATTACH ':memory:' AS {SCHEMA}")
    conn.execute(
        f"CREATE TABLE {SCHEMA}.{queries.ENROLLMENT_MV} ({', '.join(_ENROLLMENT_MV_COLUMNS)})"
    )
    conn.execute(f"CREATE TABLE {SCHEMA}.{queries.LEARNER_MV} ({', '.join(_LEARNER_MV_COLUMNS)})")

    enrollments = (
        ("consented", 42, True, CONSENT_ON),
        ("consented", 43, None, None),
        ("declined", 42, False, None),
        ("undecided", 42, None, None),
    )
    for name, contract_id, decision, consent_on in enrollments:
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
    for name, decision in (("consented", None), ("declined", False), ("undecided", None)):
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
        )
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


def test_enrollments_disclose_only_recorded_consent_when_failing_closed(conn):
    assert settings.consent_fail_open is False
    rows, withheld = _enrollments(conn)
    assert {key: row["grade"] for key, row in rows.items()} == {
        ("consented", 42): 0.8,
        ("consented", 43): None,
        ("declined", 42): None,
        ("undecided", 42): None,
    }
    assert withheld == 3


def test_enrollments_withhold_a_recorded_decline_when_failing_open(conn, monkeypatch):
    monkeypatch.setattr(settings, "consent_fail_open", True)
    rows, withheld = _enrollments(conn)
    assert {key: bool(row["outcomes_shared"]) for key, row in rows.items()} == {
        ("consented", 42): True,
        ("consented", 43): True,
        ("declined", 42): False,
        ("undecided", 42): True,
    }
    assert rows["declined", 42]["grade"] is None
    assert withheld == 1


def test_status_filter_follows_the_recorded_decision(conn, monkeypatch):
    monkeypatch.setattr(settings, "consent_fail_open", True)
    # The decliner passed too, and `passed` must not say so.
    passed, _ = _enrollments(conn, completion_statuses=("passed",))
    assert ("declined", 42) not in passed
    unknown, _ = _enrollments(conn, completion_statuses=("unknown",))
    assert set(unknown) == {("declined", 42)}


@pytest.mark.parametrize("include_inactive", [False, True])
def test_learners_take_the_organization_decision(conn, monkeypatch, include_inactive):
    # The precomputed rollup and the include_inactive recompute both span every
    # contract, so they take mv_b2b_learner's decision, not one contract's.
    rows, withheld = _learners(conn, include_inactive=include_inactive)
    assert {name: row["courses_passed"] for name, row in rows.items()} == {
        "consented": None,
        "declined": None,
        "undecided": None,
    }
    assert withheld == 3

    monkeypatch.setattr(settings, "consent_fail_open", True)
    rows, withheld = _learners(conn, include_inactive=include_inactive)
    assert rows["declined"]["courses_passed"] is None
    assert rows["consented"]["courses_passed"] is not None
    assert rows["undecided"]["courses_passed"] is not None
    assert withheld == 1


def test_contract_scoped_learners_take_that_contracts_decision(conn):
    assert settings.consent_fail_open is False
    rows, withheld = _learners(conn, contract_id=42)
    # `consented` has no decision under contract 43, which this request leaves out.
    assert rows["consented"]["courses_passed"] == 1
    assert rows["consented"]["outcomes_consent_on"] == CONSENT_ON
    assert rows["declined"]["courses_passed"] is None
    assert rows["undecided"]["courses_passed"] is None
    assert withheld == 2

    rows, withheld = _learners(conn, contract_id=43)
    assert set(rows) == {"consented"}
    assert rows["consented"]["courses_passed"] is None
    assert rows["consented"]["outcomes_consent_on"] is None
    assert withheld == 1
