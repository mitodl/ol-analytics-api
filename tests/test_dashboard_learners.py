"""Tests for the b2b_dashboard learner-paged endpoint, ``/learners``.

The first half drives the mounted app with a stubbed StarRocks pool, as
test_dashboard_learner_progress.py does, and pins what the SQL binds and how
the rows nest. The second half executes ``learner_queries.learners()``
verbatim against an MV-shaped sqlite table, which is what proves the paging,
matching and ordering rules.
"""

import base64
import datetime
import json
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.core.db.refresh_metadata import _clear_cache
from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_dashboard import learner_queries
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.learner_models import (
    LearnerEnrollment,
    LearnerIdentity,
    LearnerProgress,
    LearnersResponse,
    LearnerWithEnrollments,
)
from ol_analytics_api.tenants.b2b_dashboard.routers.learners import MAX_COURSERUN_FILTERS

ORG_ID = "11111111-1111-1111-1111-111111111111"
CONTRACT_ID = 101
PATH = f"/api/v1/analytics/organizations/{ORG_ID}/contracts/{CONTRACT_ID}/learners"
_AS_OF = datetime.datetime(2026, 9, 15, 6, 0)  # noqa: DTZ001 - StarRocks returns naive UTC
_TODAY = datetime.date(2026, 9, 15)
_TODAY_SQL = "'2026-09-15'"


def _manager_header(organization_id=ORG_ID):
    claims = {"sub": "kc-uuid-1", "organization": {"an-alias": {"id": organization_id}}}
    return base64.b64encode(json.dumps(claims).encode()).decode()


def _row(**overrides):
    return {
        "learner_id": "3e1a9c74-5b2d-4f88-9a01-7c6de2b4f019",
        "email": "rgarcia@contoso.example",
        "full_name": "R. Garcia",
        "courserun_readable_id": "course-v1:MITxT+14.310x+2T2026",
        "courserun_title": "Data Analysis for Social Scientists",
        "courserun_start_on": "2026-02-01T00:00:00",
        "courserun_end_on": None,
        "enrolled_on": "2026-02-03T14:22:11.000000",
        "enrollment_is_active": 1,
        "enrollment_mode": "verified",
        "outcomes_shared": 1,
        "outcomes_consent_status": "consented",
        "completion_status": "in_progress",
        "is_passing": 0,
        "grade": 0.4,
        "letter_grade": None,
        "content_progress": None,
        "certificate_issued_on": None,
        "certificate_is_revoked": None,
        "last_active_on": "2026-09-01",
        "needs_attention": 0,
        **overrides,
    }


class _FakePool:
    """Answers the UTC date, as_of, contract-gate, count and page queries,
    recording every call."""

    def __init__(self, rows=(), total_count=0, *, contract_exists=True):
        self.rows = list(rows)
        self.total_count = total_count
        self.contract_exists = contract_exists
        self.calls = []

    async def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        if query == learner_queries.UTC_TODAY_QUERY:
            return [{"today": _TODAY}]
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if query.startswith("SELECT 1 "):
            return [{"1": 1}] if self.contract_exists else []
        if "COUNT(" in query:
            return [{"total_count": self.total_count}]
        return self.rows

    def page_call(self):
        return next(call for call in self.calls if call[0].startswith("WITH records AS ("))

    def count_call(self):
        return next(call for call in self.calls if "COUNT(" in call[0])


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _clear_as_of_cache():
    _clear_cache()
    yield
    _clear_cache()


async def _get(app, pool, *, is_manager=True, params=None):
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=pool.fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=is_manager),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(PATH, params=params, headers={"X-Userinfo": _manager_header()})


async def test_rows_nest_into_learners_under_a_learner_count(app):
    rows = [
        _row(learner_id="a", email="a@x", full_name="Ann", courserun_readable_id="run-1"),
        _row(learner_id="a", email="a@x", full_name="Ann", courserun_readable_id="run-2"),
        _row(learner_id="b", email="b@x", full_name=None, courserun_readable_id="run-1"),
    ]
    response = await _get(app, _FakePool(rows=rows, total_count=40))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"organization_id", "as_of", "total_count", "data"}
    assert body["total_count"] == 40
    assert [(learner["learner_id"], learner["full_name"]) for learner in body["data"]] == [
        ("a", "Ann"),
        ("b", None),
    ]
    assert [
        [enrollment["courserun_readable_id"] for enrollment in learner["enrollments"]]
        for learner in body["data"]
    ] == [["run-1", "run-2"], ["run-1"]]
    # Identity lives on the learner, not repeated on each enrollment.
    assert set(body["data"][0]["enrollments"][0]) == set(LearnerEnrollment.model_fields)


async def test_a_withheld_enrollment_is_gated_inside_the_nesting(app):
    # The query already nulls these; the validator is the second check.
    rows = [_row(outcomes_shared=0, completion_status="passed", grade=0.9, needs_attention=1)]
    enrollment = (await _get(app, _FakePool(rows=rows))).json()["data"][0]["enrollments"][0]
    assert (enrollment["completion_status"], enrollment["grade"]) == (None, None)
    assert enrollment["needs_attention"] is None


async def test_limit_and_offset_bind_last_and_count_learners(app):
    pool = _FakePool()
    await _get(app, pool, params={"search": "garcia", "completion_status": ["passed"]})
    _, page_params = pool.page_call()
    _, count_params = pool.count_call()
    assert page_params == (*count_params, settings.default_learner_page_size, 0)
    assert count_params == (ORG_ID, CONTRACT_ID, "%garcia%", "%garcia%", "passed")

    pool = _FakePool()
    await _get(app, pool, params={"limit": 25, "offset": 50})
    assert pool.page_call()[1][-2:] == (25, 50)


async def test_modules_bind_as_an_in_list_on_page_and_count(app):
    pool = _FakePool()
    await _get(app, pool, params={"courserun_readable_id": ["run-1", "run-2"]})
    for query, params in (pool.page_call(), pool.count_call()):
        assert "courserun_readable_id IN (%s, %s)" in query
        assert params[2:4] == ("run-1", "run-2")


@pytest.mark.parametrize(
    ("limit", "status"),
    [
        (settings.max_learner_page_size, 200),
        (settings.max_learner_page_size + 1, 422),
        (0, 422),
    ],
)
async def test_page_size_is_capped_by_the_learner_setting(app, limit, status):
    response = await _get(app, _FakePool(), params={"limit": limit})
    assert response.status_code == status


async def test_page_and_count_share_one_filter_and_one_date(app):
    pool = _FakePool()
    await _get(app, pool, params={"needs_attention": "true", "completion_status": ["unknown"]})
    todays = [i for i, (q, _) in enumerate(pool.calls) if q == learner_queries.UTC_TODAY_QUERY]
    page_query, _ = pool.page_call()
    count_query, _ = pool.count_call()
    assert len(todays) == 1
    assert todays[0] < pool.calls.index(pool.page_call())
    attention = learner_queries._needs_attention(_TODAY)  # noqa: SLF001
    for query in (page_query, count_query):
        assert "CURRENT_DATE" not in query
        assert _TODAY_SQL in query
        assert f"(COALESCE(outcomes_decision, FALSE) AND ({attention}))" in query
        assert "NOT COALESCE(outcomes_decision, FALSE)" in query
        assert "enrollment_is_active = TRUE" in query
    # The filter picks learners; the final select joins back to every row.
    assert "FROM records JOIN paged" in page_query


@pytest.mark.parametrize(
    "params",
    [
        {"needs_attention": "maybe"},
        {"completion_status": ["finished"]},
        {"search": ""},
        {"courserun_readable_id": [""]},
        {"courserun_readable_id": [f"run-{i}" for i in range(MAX_COURSERUN_FILTERS + 1)]},
    ],
)
async def test_invalid_filters_are_rejected(app, params):
    pool = _FakePool()
    response = await _get(app, pool, params=params)
    assert response.status_code == 422
    # The router-level contract gate runs before query validation, so only the
    # learner data itself is guaranteed untouched.
    assert all(learner_queries.ENROLLMENT_MV not in query for query, _ in pool.calls)


async def test_non_manager_is_refused_before_any_query(app):
    pool = _FakePool()
    assert (await _get(app, pool, is_manager=False)).status_code == 403
    assert pool.calls == []


async def test_contract_outside_the_org_is_refused(app):
    pool = _FakePool(contract_exists=False)
    assert (await _get(app, pool)).status_code == 403
    assert all(learner_queries.ENROLLMENT_MV not in query for query, _ in pool.calls)


def test_learner_progress_is_exactly_identity_plus_enrollment():
    assert set(LearnerProgress.model_fields) == (
        set(LearnerIdentity.model_fields) | set(LearnerEnrollment.model_fields)
    )
    # Identity first, so the published schema keeps its field order.
    assert list(LearnerProgress.model_fields)[:3] == ["learner_id", "email", "full_name"]


@pytest.mark.parametrize("model", [LearnerWithEnrollments, LearnersResponse])
def test_every_field_has_a_manager_facing_description(model):
    field_names = (
        set(LearnerProgress.model_fields)
        | set(LearnerWithEnrollments.model_fields)
        | set(LearnersResponse.model_fields)
    )
    for name, field in model.model_fields.items():
        assert field.description, f"{name} has no description"
        named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", field.description)) & field_names
        assert not named, f"{name}'s description names {named}"


def test_from_rows_never_emits_a_learner_twice():
    rows = [
        _row(learner_id="a", courserun_readable_id="run-1"),
        _row(learner_id="b", courserun_readable_id="run-1"),
        _row(learner_id="a", courserun_readable_id="run-2"),
    ]
    learners = LearnerWithEnrollments.from_rows(rows)
    assert [(learner.learner_id, len(learner.enrollments)) for learner in learners] == [
        ("a", 2),
        ("b", 1),
    ]


# --- Executed against an MV-shaped sqlite table -------------------------------

_ENROLLMENT_MV_COLUMNS = (
    "user_pk",
    "courserun_pk",
    "user_global_id",
    "email",
    "full_name",
    "sso_organization_id",
    "contract_id",
    "courserun_readable_id",
    "courserun_title",
    "courserun_start_on",
    "courserun_end_on",
    "enrollment_created_on",
    "enrollment_is_active",
    "enrollment_mode",
    "completion_status",
    "certificate_is_revoked",
    "is_passing",
    "grade_value",
    "letter_grade",
    "content_progress",
    "certificate_issued_on",
    "last_active_on",
    "needs_attention_since",
    "outcomes_shared",
)

# needs_attention_since as the view dates it: 30 days after the last activity
# while in progress, the enrollment date when not started, none once passed.
_PASSED = {
    "completion_status": "passed",
    "is_passing": True,
    "grade_value": 0.9,
    "last_active_on": "2026-09-01",
}
_RECENT = {
    "completion_status": "in_progress",
    "grade_value": 0.3,
    "last_active_on": "2026-09-01",
    "needs_attention_since": "2026-10-01",
}
# Due on or before _TODAY, so an enrollment like this needs attention.
_STALE = {
    "completion_status": "in_progress",
    "grade_value": 0.2,
    "last_active_on": "2026-07-01",
    "needs_attention_since": "2026-07-31",
}
_NOT_STARTED = {"completion_status": "not_started", "needs_attention_since": "2026-02-03"}

# learner_id: (full_name, email, consent decision, [(run, outcome)]).
_LEARNERS = {
    "ann": (
        "Ann Lee",
        "ann@x",
        True,
        [("run-1", _PASSED), ("run-2", _STALE), ("run-3", _NOT_STARTED)],
    ),
    "bob": ("Bob Ray", "bob@x", True, [("run-1", _RECENT), ("run-2", _PASSED)]),
    "eve": ("Eve Ng", "eve@x", False, [("run-1", _PASSED), ("run-2", _PASSED)]),
    # Same name and email: only learner_id can order them.
    "gus-a": ("Gus", "gus@x", True, [("run-1", _NOT_STARTED)]),
    "gus-b": ("Gus", "gus@x", True, [("run-2", _NOT_STARTED)]),
    # A blank name is no name, so these sort last, by email.
    "dee": (None, "dee@x", True, [("run-1", _RECENT)]),
    "cy": ("  ", "cy@x", True, [("run-1", _RECENT)]),
}
_ALL_ORDER = ["ann", "bob", "eve", "gus-a", "gus-b", "cy", "dee"]


@pytest.fixture
def mv(monkeypatch):
    monkeypatch.setattr(settings, "consent_fail_open", False)
    schema = settings.learner_records_schema
    table = f"{schema}.{learner_queries.ENROLLMENT_MV}"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH ':memory:' AS {schema}")
    conn.execute(f"CREATE TABLE {table} ({', '.join(_ENROLLMENT_MV_COLUMNS)})")

    def insert(learner, run, outcome, *, active=True):
        learner_id, full_name, email, decision = learner
        row = {
            "user_pk": learner_id,
            "courserun_pk": run,
            "user_global_id": learner_id,
            "email": email,
            "full_name": full_name,
            "sso_organization_id": ORG_ID,
            "contract_id": CONTRACT_ID,
            "courserun_readable_id": run,
            "courserun_title": run,
            "enrollment_created_on": "2026-02-03T00:00:00",
            "enrollment_is_active": active,
            "outcomes_shared": decision,
            **outcome,
        }
        conn.execute(
            f"INSERT INTO {table} VALUES ({', '.join('?' * len(_ENROLLMENT_MV_COLUMNS))})",  # noqa: S608
            [row.get(column) for column in _ENROLLMENT_MV_COLUMNS],
        )

    for learner_id, (full_name, email, decision, enrollments) in _LEARNERS.items():
        for run, outcome in enrollments:
            insert((learner_id, full_name, email, decision), run, outcome)
    # Inactive only: never listed, and never a reason to list anyone.
    insert(("fay", "Fay", "fay@x", True), "run-1", _STALE, active=False)
    insert(("bob", "Bob Ray", "bob@x", True), "run-9", _STALE, active=False)
    yield SimpleNamespace(conn=conn, insert=insert)
    conn.close()


def _run(mv, *, limit=100, offset=0, **filters):
    query = learner_queries.learners(
        learner_queries.LearnerFilters(organization_id=ORG_ID, contract_id=CONTRACT_ID, **filters),
        _TODAY,
    )
    rows = [
        dict(row)
        for row in mv.conn.execute(query.page.replace("%s", "?"), (*query.params, limit, offset))
    ]
    total = mv.conn.execute(query.count.replace("%s", "?"), query.params).fetchone()["total_count"]
    return LearnerWithEnrollments.from_rows(rows), total


def _shape(learners):
    return {
        learner.learner_id: [e.courserun_readable_id for e in learner.enrollments]
        for learner in learners
    }


def test_learners_sort_by_name_nulls_last_then_email_then_id(mv):
    learners, total = _run(mv)
    assert [learner.learner_id for learner in learners] == _ALL_ORDER
    assert total == len(_ALL_ORDER)
    assert _shape(learners)["ann"] == ["run-1", "run-2", "run-3"]
    # Inactive enrollments are neither listed nor counted.
    assert _shape(learners)["bob"] == ["run-1", "run-2"]


def test_pages_split_learners_never_enrollments(mv):
    everyone = _shape(_run(mv)[0])
    seen = []
    for offset in range(0, len(_ALL_ORDER), 2):
        learners, total = _run(mv, limit=2, offset=offset)
        assert total == len(_ALL_ORDER)
        for learner_id, runs in _shape(learners).items():
            assert runs == everyone[learner_id]
            seen.append(learner_id)
    assert seen == _ALL_ORDER
    assert _run(mv, limit=2, offset=20) == ([], len(_ALL_ORDER))


def test_a_match_returns_every_enrollment_of_the_learner(mv):
    learners, total = _run(mv, completion_statuses=("passed",))
    # eve passed too, but withheld her outcomes, so the filter can't match her.
    assert _shape(learners) == {"ann": ["run-1", "run-2", "run-3"], "bob": ["run-1", "run-2"]}
    assert total == 2
    statuses = [e.completion_status for e in learners[0].enrollments]
    assert statuses == ["passed", "in_progress", "not_started"]


def test_filters_combine_on_one_enrollment(mv):
    # ann passed run-1 and is quiet in run-2, but no single enrollment is both.
    assert _run(mv, completion_statuses=("passed",), needs_attention=True) == ([], 0)
    learners, total = _run(mv, completion_statuses=("in_progress",), needs_attention=True)
    assert list(_shape(learners)) == ["ann"]
    assert total == 1
    learners, _ = _run(mv, needs_attention=True)
    assert list(_shape(learners)) == ["ann", "gus-a", "gus-b"]


def test_one_module_lists_learners_with_every_enrollment(mv):
    learners, total = _run(mv, courserun_readable_ids=("run-3",))
    assert _shape(learners) == {"ann": ["run-1", "run-2", "run-3"]}
    assert total == 1


def test_several_modules_or_together_and_count_learners(mv):
    learners, total = _run(mv, courserun_readable_ids=("run-1", "run-2"))
    # ann and bob match both runs but count once; eve is listed (her rows exist).
    assert list(_shape(learners)) == _ALL_ORDER
    assert total == len(_ALL_ORDER)


def test_an_unknown_module_lists_nobody(mv):
    assert _run(mv, courserun_readable_ids=("run-404",)) == ([], 0)
    # bob's run-9 enrollment is inactive, so it is no match.
    assert _run(mv, courserun_readable_ids=("run-9",)) == ([], 0)


def test_modules_compose_with_status_and_needs_attention(mv):
    learners, total = _run(mv, courserun_readable_ids=("run-1",), completion_statuses=("passed",))
    assert list(_shape(learners)) == ["ann"]
    assert total == 1
    # ann's quiet enrollment is run-2, so run-1 plus needs_attention skips her.
    learners, _ = _run(mv, courserun_readable_ids=("run-1",), needs_attention=True)
    assert list(_shape(learners)) == ["gus-a"]
    learners, _ = _run(mv, courserun_readable_ids=("run-2",), needs_attention=True)
    assert list(_shape(learners)) == ["ann", "gus-b"]


def test_search_lists_the_learner_with_all_their_enrollments(mv):
    learners, total = _run(mv, search="BOB")
    assert _shape(learners) == {"bob": ["run-1", "run-2"]}
    assert total == 1


@pytest.mark.parametrize(("fail_open", "withheld"), [(False, {"eve", "hal"}), (True, {"eve"})])
def test_withheld_learners_are_reachable_only_through_unknown(mv, monkeypatch, fail_open, withheld):
    monkeypatch.setattr(settings, "consent_fail_open", fail_open)
    mv.insert(("hal", "Hal", "hal@x", None), "run-1", _PASSED)
    learners, total = _run(mv, completion_statuses=("unknown",))
    assert set(_shape(learners)) == withheld
    assert total == len(withheld)
    for learner in learners:
        assert all(e.completion_status is None and e.grade is None for e in learner.enrollments)
    passed, _ = _run(mv, completion_statuses=("passed",))
    assert not set(_shape(passed)) & withheld
    for needs_attention in (True, False):
        learners, _ = _run(mv, needs_attention=needs_attention)
        assert not set(_shape(learners)) & withheld


def test_the_projection_is_exactly_the_row_model(mv):
    # LearnerEnrollment ignores unknown keys, so a column added to the query
    # alone would vanish silently; pin the two together.
    query = learner_queries.learners(
        learner_queries.LearnerFilters(organization_id=ORG_ID, contract_id=CONTRACT_ID), _TODAY
    )
    cursor = mv.conn.execute(query.page.replace("%s", "?"), (*query.params, 100, 0))
    assert {column[0] for column in cursor.description} == set(LearnerProgress.model_fields)
