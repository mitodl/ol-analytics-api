"""Tests for the b2b_dashboard learner-paged endpoint, ``/learners``.

The first half drives the mounted app with a stubbed StarRocks pool, as
test_dashboard_learner_progress.py does, and pins what the SQL binds and how
the rows nest. The second half executes ``learner_queries.learners()``
verbatim against an MV-shaped sqlite table, which is what proves the paging,
matching and ordering rules.
"""

import base64
import datetime as dt
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
    RunEnrollment,
)
from ol_analytics_api.tenants.b2b_dashboard.routers.learners import (
    MAX_COURSERUN_FILTERS,
    CompletionStatusFilter,
    LearnerStatusFilter,
)
from tests.membership import answer_membership_probe, is_membership_probe

ORG_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
CONTRACT_ID = 101
PATH = f"/api/v1/analytics/organizations/{ORG_ID}/contracts/{CONTRACT_ID}/learners"
_AS_OF = dt.datetime(2026, 9, 15, 6, 0)  # noqa: DTZ001 - StarRocks returns naive UTC
_TODAY = dt.date(2026, 9, 15)
_TODAY_SQL = "'2026-09-15'"


def _manager_header(organization_id=ORG_ID):
    claims = {"sub": "kc-uuid-1", "organization": {"an-alias": {"id": organization_id}}}
    return base64.b64encode(json.dumps(claims).encode()).decode()


def _row(**overrides):
    return {
        "learner_id": "3e1a9c74-5b2d-4f88-9a01-7c6de2b4f019",
        "email": "rgarcia@contoso.example",
        "full_name": "R. Garcia",
        "course_readable_id": "course-v1:MITxT+14.310x",
        "course_title": "Data Analysis for Social Scientists",
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
        "days_active": 3,
        "videos_watched": 5,
        "problems_attempted": 4,
        "chatbot_interactions": 1,
        "needs_attention": 0,
        **overrides,
    }


class _FakePool:
    """Answers the UTC date, as_of, contract-gate, count and page queries,
    recording every call."""

    def __init__(self, rows=(), total_count=0, *, loaded=((ORG_ID, CONTRACT_ID),)):
        self.rows = list(rows)
        self.total_count = total_count
        self.loaded = loaded
        self.calls = []

    async def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        if query == learner_queries.UTC_TODAY_QUERY:
            return [{"today": _TODAY}]
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if is_membership_probe(query):
            return answer_membership_probe(query, params, self.loaded)
        if "total_count" in query:
            return [{"total_count": self.total_count, "outcomes_withheld_count": 3}]
        return self.rows

    def page_call(self):
        return next(call for call in self.calls if " JOIN paged " in call[0])

    def as_of_call(self):
        return next(call for call in self.calls if "information_schema" in call[0])

    def count_call(self):
        return next(call for call in self.calls if "total_count" in call[0])


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _clear_as_of_cache():
    _clear_cache()
    yield
    _clear_cache()


async def _get(app, pool, *, is_manager=True, params=None, path=PATH):
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=pool.fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=is_manager),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, params=params, headers={"X-Userinfo": _manager_header()})


async def test_rows_nest_into_learners_under_a_learner_count(app):
    rows = [
        _row(learner_id="a", email="a@x", full_name="Ann", courserun_readable_id="run-1"),
        _row(learner_id="a", email="a@x", full_name="Ann", courserun_readable_id="run-2"),
        _row(learner_id="b", email="b@x", full_name=None, courserun_readable_id="run-1"),
    ]
    response = await _get(app, _FakePool(rows=rows, total_count=40))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "organization_id",
        "as_of",
        "total_count",
        "outcomes_withheld_count",
        "data",
    }
    assert (body["total_count"], body["outcomes_withheld_count"]) == (40, 3)
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
    # _row carries nonzero activity counts, so these nulls are the gate's.
    assert enrollment["videos_watched"] is None
    assert enrollment["chatbot_interactions"] is None


async def test_limit_and_offset_bind_last_and_count_learners(app):
    pool = _FakePool()
    await _get(app, pool, params={"search": "garcia", "completion_status": ["passed"]})
    _, page_params = pool.page_call()
    _, count_params = pool.count_call()
    assert page_params == (*count_params, settings.default_learner_page_size, 0)
    assert count_params == (
        ORG_ID,
        CONTRACT_ID,
        ORG_ID,
        CONTRACT_ID,
        "%garcia%",
        "%garcia%",
        "passed",
    )

    pool = _FakePool()
    await _get(app, pool, params={"limit": 25, "offset": 50})
    assert pool.page_call()[1][-2:] == (25, 50)


async def test_modules_bind_as_an_in_list_on_page_and_count(app):
    pool = _FakePool()
    await _get(app, pool, params={"courserun_readable_id": ["run-1", "run-2"]})
    for query, params in (pool.page_call(), pool.count_call()):
        assert "courserun_readable_id IN (%s, %s)" in query
        assert params[4:6] == ("run-1", "run-2")


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
        assert f".{learner_queries.MEMBER_MV} " in query
        # The view holds active enrollments only, and its member rows have no
        # enrollment_is_active for this to test.
        assert "enrollment_is_active = TRUE" not in query
        # learner_id is the group, page and count key, so a null one is excluded.
        assert "user_global_id IS NOT NULL" in query
    # The filter picks learners; the final select joins back to every row.
    assert "FROM records JOIN paged" in page_query
    assert pool.as_of_call()[1] == (settings.learner_records_schema, learner_queries.MEMBER_MV)


async def test_not_enrolled_is_a_status_of_learners_only(app):
    # A status added to /learner-progress's filter must reach /learners' too.
    missing = {learner_queries.NOT_ENROLLED, learner_queries.NOT_ENROLLED_IN_ANY}
    assert set(LearnerStatusFilter) == {*CompletionStatusFilter, *missing}
    params = {"completion_status": ["not_enrolled"]}
    pool = _FakePool()
    assert (await _get(app, pool, params=params)).status_code == 200
    # The modules it checks are the contract's, read in the same statement.
    for query, query_params in (pool.page_call(), pool.count_call()):
        assert " modules AS (" in query
        assert query_params[:6] == (ORG_ID, CONTRACT_ID) * 3
    # Without it, the statements don't read them.
    pool = _FakePool()
    await _get(app, pool, params={"completion_status": ["passed"]})
    assert " modules AS (" not in pool.page_call()[0]
    # /learner-progress has a row per enrollment, so nothing for it to select.
    progress = PATH.removesuffix("/learners") + "/learner-progress"
    assert (await _get(app, _FakePool(), params=params, path=progress)).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"needs_attention": "maybe"},
        {"completion_status": ["finished"]},
        {"search": ""},
        {"courserun_readable_id": [""]},
        {"courserun_readable_id": [f"run-{i}" for i in range(MAX_COURSERUN_FILTERS + 1)]},
        {"course_readable_id": [""]},
        {"course_readable_id": [f"course-{i}" for i in range(MAX_COURSERUN_FILTERS + 1)]},
    ],
)
async def test_invalid_filters_are_rejected(app, params):
    pool = _FakePool()
    response = await _get(app, pool, params=params)
    assert response.status_code == 422
    # The router-level contract gate runs before query validation, so only the
    # learner data itself is guaranteed untouched.
    assert all(learner_queries.MEMBER_MV not in query for query, _ in pool.calls)


async def test_non_manager_is_refused_before_any_query(app):
    pool = _FakePool()
    assert (await _get(app, pool, is_manager=False)).status_code == 403
    assert pool.calls == []


async def test_contract_outside_the_org_is_refused(app):
    pool = _FakePool(loaded=((OTHER_ORG_ID, CONTRACT_ID),))
    assert (await _get(app, pool)).status_code == 403
    assert all(learner_queries.MEMBER_MV not in query for query, _ in pool.calls)


def test_learner_progress_is_exactly_identity_plus_enrollment():
    assert set(LearnerProgress.model_fields) == (
        set(LearnerIdentity.model_fields) | set(RunEnrollment.model_fields)
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


def test_from_rows_lists_a_member_row_with_no_enrollment():
    member = dict.fromkeys(_row()) | {"learner_id": "n", "email": "n@x", "full_name": "Ned"}
    learners = LearnerWithEnrollments.from_rows([_row(learner_id="a"), member])
    assert [(learner.learner_id, len(learner.enrollments)) for learner in learners] == [
        ("a", 1),
        ("n", 0),
    ]


# --- Executed against an MV-shaped sqlite table -------------------------------

_MEMBER_MV_COLUMNS = (
    "user_pk",
    "courserun_pk",
    "user_global_id",
    "email",
    "full_name",
    "sso_organization_id",
    "contract_id",
    "course_readable_id",
    "course_title",
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
    "days_active",
    "videos_played",
    "problems_attempted",
    "chatbot_interactions",
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

# The contract's runs, as the Learners page shows them, each the one run of its
# course (run-1 of course-1, ...), so a run and its course select the same entries.
_MODULES = ("run-1", "run-2", "run-3")


def _course(run):
    return run.replace("run-", "course-")


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
    # No name (the view publishes a blank one as null), so these sort last, by email.
    "dee": (None, "dee@x", True, [("run-1", _RECENT)]),
    "cy": (None, "cy@x", True, [("run-1", _RECENT)]),
}
_ALL_ORDER = ["ann", "bob", "eve", "gus-a", "gus-b", "cy", "dee"]


@pytest.fixture
def mv(monkeypatch):
    monkeypatch.setattr(settings, "consent_fail_open", False)
    schema = settings.learner_records_schema
    table = f"{schema}.{learner_queries.MEMBER_MV}"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH ':memory:' AS {schema}")
    conn.execute(f"CREATE TABLE {table} ({', '.join(_MEMBER_MV_COLUMNS)})")
    # The contract's modules, which not_enrolled checks. Another contract's
    # module must not count.
    modules = f"{schema}.{learner_queries.CONTRACT_COURSERUN_MV}"
    conn.execute(
        f"CREATE TABLE {modules} (sso_organization_id, contract_id, courserun_readable_id,"
        " course_readable_id, course_order_in_program)"
    )

    def listed(run, course, order=1, contract_id=CONTRACT_ID):
        """A contract run; order None puts its course in none of the contract's programs."""
        conn.execute(
            f"INSERT INTO {modules} VALUES (?, ?, ?, ?, ?)",  # noqa: S608
            (ORG_ID, contract_id, run, course, order),
        )

    for order, run in enumerate(_MODULES, start=1):
        listed(run, _course(run), order)
    listed("run-8", "course-8", contract_id=CONTRACT_ID + 1)

    def member(learner, **enrollment):
        """With no enrollment, the view's row for a member with none."""
        learner_id, full_name, email, decision = learner
        row = {
            "user_pk": learner_id,
            "user_global_id": learner_id,
            "email": email,
            "full_name": full_name,
            "sso_organization_id": ORG_ID,
            "contract_id": CONTRACT_ID,
            "outcomes_shared": decision,
            **enrollment,
        }
        conn.execute(
            f"INSERT INTO {table} VALUES ({', '.join('?' * len(_MEMBER_MV_COLUMNS))})",  # noqa: S608
            [row.get(column) for column in _MEMBER_MV_COLUMNS],
        )

    def insert(learner, run, outcome):
        """``outcome`` can also set the run's course and dates."""
        enrollment = {
            "courserun_pk": run,
            "courserun_readable_id": run,
            "courserun_title": run,
            "course_readable_id": _course(run),
            "course_title": _course(run),
            "enrollment_created_on": "2026-02-03T00:00:00",
            "enrollment_is_active": True,
        }
        member(learner, **(enrollment | outcome))

    # The view holds active enrollments only, so there are no inactive ones here.
    for learner_id, (full_name, email, decision, enrollments) in _LEARNERS.items():
        for run, outcome in enrollments:
            insert((learner_id, full_name, email, decision), run, outcome)
    yield SimpleNamespace(conn=conn, insert=insert, member=member, listed=listed)
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


def _counts(mv, **filters):
    query = learner_queries.learners(
        learner_queries.LearnerFilters(organization_id=ORG_ID, contract_id=CONTRACT_ID, **filters),
        _TODAY,
    )
    row = mv.conn.execute(query.count.replace("%s", "?"), query.params).fetchone()
    return row["total_count"], row["outcomes_withheld_count"]


def test_withheld_count_counts_learners_and_agrees_with_unknown(mv):
    mv.insert(("hal", "Hal", "hal@x", None), "run-1", _PASSED)
    # eve has two withheld enrollments but counts once; hal is undecided.
    assert _counts(mv) == (len(_ALL_ORDER) + 1, 2)
    unknown_total, unknown_withheld = _counts(mv, completion_statuses=("unknown",))
    assert unknown_total == unknown_withheld == 2
    assert _counts(mv, courserun_readable_ids=("run-2",)) == (4, 1)
    assert _counts(mv, search="eve") == (1, 1)


@pytest.mark.parametrize(
    "filters",
    [
        {"completion_statuses": ("passed",)},
        {"completion_statuses": ("in_progress", "not_started")},
        {"needs_attention": True},
        {"needs_attention": False},
    ],
)
def test_withheld_count_is_zero_under_outcome_filters(mv, filters):
    assert _counts(mv, **filters)[1] == 0


# A member with no active enrollment and no consent recorded.
_NED = ("ned", "Ned Ma", "ned@x", None)
_NOT_ENROLLED = (learner_queries.NOT_ENROLLED,)
_NOT_ENROLLED_IN_ANY = (learner_queries.NOT_ENROLLED_IN_ANY,)


@pytest.mark.parametrize("decision", [True, False, None])
def test_a_member_with_no_enrollment_is_listed_with_none(mv, decision):
    mv.member(("ned", "Ned Ma", "ned@x", decision))
    learners, total = _run(mv)
    # Sorted by name like everyone else: after the Gus pair, before the nameless.
    assert [learner.learner_id for learner in learners] == [*_ALL_ORDER[:5], "ned", *_ALL_ORDER[5:]]
    assert _shape(learners)["ned"] == []
    # There is no outcome to withhold, whatever consent the row carries.
    assert (total, _counts(mv)[1]) == (len(_ALL_ORDER) + 1, 1)
    assert "ned" not in _shape(_run(mv, completion_statuses=("unknown",))[0])
    assert _shape(_run(mv, search="NED")[0]) == {"ned": []}


@pytest.mark.parametrize("decision", [True, False, None])
@pytest.mark.parametrize(
    "filters",
    [
        {"completion_statuses": ("not_started", "in_progress", "passed", "certified", "unknown")},
        {"needs_attention": True},
        {"needs_attention": False},
        {"courserun_readable_ids": ("run-1",)},
    ],
)
def test_a_member_row_matches_no_enrollment_filter(mv, decision, filters):
    # A consented member row reads as shared, with no needs_attention_since,
    # so only the course-run guard keeps needs_attention=false off it.
    mv.member(("ned", "Ned Ma", "ned@x", decision))
    assert "ned" not in _shape(_run(mv, **filters)[0])


def test_not_enrolled_is_a_missing_enrollment_in_any_selected_module(mv):
    mv.member(_NED)
    # ann is the only learner enrolled in run-3. eve withheld her outcomes,
    # but whether she enrolled isn't an outcome.
    missing_run_3 = ["bob", "eve", "gus-a", "gus-b", "ned", "cy", "dee"]
    learners, total = _run(mv, completion_statuses=_NOT_ENROLLED, courserun_readable_ids=("run-3",))
    assert (list(_shape(learners)), total) == (missing_run_3, 7)
    # Several modules: missing any one of them, as the other statuses match
    # any one. gus-a has run-1 but not run-2; ann, bob and eve have both.
    learners, total = _run(
        mv, completion_statuses=_NOT_ENROLLED, courserun_readable_ids=("run-1", "run-2")
    )
    assert (list(_shape(learners)), total) == (["gus-a", "gus-b", "ned", "cy", "dee"], 5)
    # No module selected means every module on the contract, where only ann
    # has all three. Another contract's run-8 isn't one of them.
    learners, total = _run(mv, completion_statuses=_NOT_ENROLLED)
    assert (list(_shape(learners)), total) == (missing_run_3, 7)


@pytest.mark.parametrize("decision", [True, False, None])
def test_not_enrolled_in_any_is_no_enrollment_in_any_selected_module(mv, decision):
    mv.member(("ned", "Ned Ma", "ned@x", decision))
    # With no module selected, every module on the contract: only ned has none.
    assert _shape(_run(mv, completion_statuses=_NOT_ENROLLED_IN_ANY)[0]) == {"ned": []}
    # One module: the same learners as not_enrolled.
    for run in _MODULES:
        assert _run(
            mv, completion_statuses=_NOT_ENROLLED_IN_ANY, courserun_readable_ids=(run,)
        ) == _run(mv, completion_statuses=_NOT_ENROLLED, courserun_readable_ids=(run,))
    # Several: missing every one of them. gus-b has run-2; ann has both.
    learners, total = _run(
        mv, completion_statuses=_NOT_ENROLLED_IN_ANY, courserun_readable_ids=("run-2", "run-3")
    )
    assert (list(_shape(learners)), total) == (["gus-a", "ned", "cy", "dee"], 4)
    # Every course selected, as the dashboard tile links, is the same as none.
    every_course = tuple(_course(run) for run in _MODULES)
    assert _run(mv, completion_statuses=_NOT_ENROLLED_IN_ANY, course_readable_ids=every_course) == (
        _run(mv, completion_statuses=_NOT_ENROLLED_IN_ANY)
    )


def test_not_enrolled_in_any_counts_a_learner_only_in_unlisted_courses(mv):
    # course-u has a contract run but is in none of its programs, so uma has no entry.
    mv.listed("u-1", "course-u", order=None)
    mv.insert(("uma", "Uma", "uma@x", True), "u-1", {"course_readable_id": "course-u", **_PASSED})
    assert _shape(_run(mv, completion_statuses=_NOT_ENROLLED_IN_ANY)[0]) == {"uma": []}


@pytest.mark.parametrize("missing", [_NOT_ENROLLED, _NOT_ENROLLED_IN_ANY])
def test_not_enrolled_ignores_modules_not_on_the_contract(mv, missing):
    mv.member(_NED)
    # No column for run-404 or run-8, so nobody can be missing them.
    for modules in (("run-404",), ("run-8",)):
        assert _run(mv, completion_statuses=missing, courserun_readable_ids=modules) == ([], 0)
    learners, _ = _run(mv, completion_statuses=missing, courserun_readable_ids=("run-3", "run-404"))
    assert "ann" not in _shape(learners)


def test_not_enrolled_ors_with_the_other_statuses(mv):
    mv.member(_NED)
    learners, total = _run(
        mv, completion_statuses=("passed", *_NOT_ENROLLED), courserun_readable_ids=("run-1",)
    )
    # ann passed run-1; gus-b and ned have no run-1 enrollment; eve's pass is withheld.
    assert (list(_shape(learners)), total) == (["ann", "gus-b", "ned"], 3)
    # Every status at once lists every member, as no filter does.
    every = (
        *("not_started", "in_progress", "passed", "certified", "unknown"),
        *_NOT_ENROLLED,
        *_NOT_ENROLLED_IN_ANY,
    )
    assert _run(mv, completion_statuses=every) == _run(mv)
    for modules in (("run-1",), ("run-1", "run-3")):
        learners, total = _run(mv, completion_statuses=every, courserun_readable_ids=modules)
        assert (list(_shape(learners)), total) == ([*_ALL_ORDER[:5], "ned", *_ALL_ORDER[5:]], 8)


@pytest.mark.parametrize("missing", [_NOT_ENROLLED, _NOT_ENROLLED_IN_ANY])
@pytest.mark.parametrize("needs_attention", [True, False])
def test_needs_attention_never_matches_not_enrolled(mv, needs_attention, missing):
    mv.member(_NED)
    assert _run(mv, completion_statuses=missing, needs_attention=needs_attention) == ([], 0)
    # It still narrows the other statuses: ann's in-progress run-2 is quiet.
    learners, _ = _run(
        mv, completion_statuses=("in_progress", *missing), needs_attention=needs_attention
    )
    assert list(_shape(learners)) == (["ann"] if needs_attention else ["bob", "cy", "dee"])


def test_the_projection_is_exactly_the_row_model(mv):
    # LearnerEnrollment ignores unknown keys, so a column added to the query
    # alone would vanish silently; pin the two together.
    query = learner_queries.learners(
        learner_queries.LearnerFilters(organization_id=ORG_ID, contract_id=CONTRACT_ID), _TODAY
    )
    cursor = mv.conn.execute(query.page.replace("%s", "?"), (*query.params, 100, 0))
    assert {column[0] for column in cursor.description} == (
        set(LearnerIdentity.model_fields) | set(LearnerEnrollment.model_fields)
    )


# --- One entry per course (mitodl/hq#14464) ------------------------------------
#
# course-x has every kind of run. _TODAY is 2026-09-15.
_X_RUNS = {
    "x-ended-early": ("2025-01-01T00:00:00", "2025-06-01T00:00:00"),
    "x-ended-late": ("2026-01-01T00:00:00", "2026-06-01T00:00:00"),
    "x-open": ("2026-08-01T00:00:00", "2026-12-01T00:00:00"),
    "x-open-late": ("2026-09-01T00:00:00", None),
    "x-ends-today": ("2026-07-01T00:00:00", "2026-09-15T18:00:00"),
    "x-starts-today": ("2026-09-15T12:00:00", None),
    "x-no-dates": (None, None),
    "x-soon": ("2026-10-01T00:00:00", None),
    "x-later": ("2027-01-01T00:00:00", None),
}
_CERTIFIED = {
    "completion_status": "certified",
    "is_passing": True,
    "grade_value": 0.95,
    "certificate_issued_on": "2025-06-02T00:00:00",
}
_ZED = ("zed", "Zed", "zed@x", True)


@pytest.fixture
def course_x(mv):
    for run in _X_RUNS:
        mv.listed(run, "course-x", order=4)

    def enroll(runs, learner=_ZED):
        for run, outcome in runs:
            start, end = _X_RUNS[run]
            mv.insert(
                learner,
                run,
                {
                    "course_readable_id": "course-x",
                    "courserun_start_on": start,
                    "courserun_end_on": end,
                    **outcome,
                },
            )

    return enroll


@pytest.mark.parametrize(
    ("runs", "kept"),
    [
        # A certificate from any run wins, even an ended one.
        ([("x-ended-early", _CERTIFIED), ("x-open", _RECENT)], "x-ended-early"),
        ([("x-ended-early", _CERTIFIED), ("x-ended-late", _CERTIFIED)], "x-ended-late"),
        ([("x-ended-early", _CERTIFIED), ("x-soon", _CERTIFIED)], "x-soon"),
        # Then the run open now, the latest start first and no start last.
        ([("x-ended-late", _PASSED), ("x-open", _RECENT), ("x-soon", _NOT_STARTED)], "x-open"),
        ([("x-open", _RECENT), ("x-open-late", _RECENT)], "x-open-late"),
        ([("x-open", _RECENT), ("x-no-dates", _RECENT)], "x-open"),
        # Day-grained: a run starting or ending today is open.
        ([("x-starts-today", _NOT_STARTED), ("x-soon", _NOT_STARTED)], "x-starts-today"),
        ([("x-ends-today", _RECENT), ("x-soon", _NOT_STARTED)], "x-ends-today"),
        # Then the next to start, then the ended run that started last.
        (
            [("x-later", _NOT_STARTED), ("x-soon", _NOT_STARTED), ("x-ended-late", _PASSED)],
            "x-soon",
        ),
        ([("x-ended-early", _PASSED), ("x-ended-late", _STALE)], "x-ended-late"),
    ],
)
def test_a_course_shows_the_run_the_overview_picks(mv, course_x, runs, kept):
    course_x(runs)
    [zed] = _run(mv, search="zed")[0]
    assert [(e.course_readable_id, e.courserun_readable_id) for e in zed.enrollments] == [
        ("course-x", kept)
    ]


def test_a_withheld_certificate_doesnt_pick_the_run(mv, course_x):
    course_x([("x-ended-early", _CERTIFIED), ("x-open", _RECENT)], ("zed", "Zed", "zed@x", False))
    [zed] = _run(mv, search="zed")[0]
    assert [e.courserun_readable_id for e in zed.enrollments] == ["x-open"]
    assert zed.enrollments[0].completion_status is None


def test_filters_read_the_kept_run(mv, course_x):
    # zed's kept run is the certified one; the quiet not-started run is hidden.
    course_x([("x-ended-early", _CERTIFIED), ("x-open", _NOT_STARTED)])
    matches = {
        "certified": _run(mv, completion_statuses=("certified",))[0],
        "not_started": _run(mv, completion_statuses=("not_started",), search="zed")[0],
        "needs_attention": _run(mv, needs_attention=True, search="zed")[0],
        "kept run": _run(mv, courserun_readable_ids=("x-ended-early",))[0],
        "other run": _run(mv, courserun_readable_ids=("x-open",))[0],
        "course": _run(mv, course_readable_ids=("course-x",))[0],
    }
    assert {name: list(_shape(learners)) for name, learners in matches.items()} == {
        "certified": ["zed"],
        "not_started": [],
        "needs_attention": [],
        "kept run": ["zed"],
        "other run": [],
        "course": ["zed"],
    }


def test_a_course_filter_matches_its_runs_filter(mv):
    for run in _MODULES:
        assert _run(mv, course_readable_ids=(_course(run),)) == _run(
            mv, courserun_readable_ids=(run,)
        )
    assert _run(mv, course_readable_ids=("course-404",)) == ([], 0)


@pytest.mark.parametrize("course", ["course-u", None])
def test_a_course_off_the_list_has_no_entry_but_keeps_the_learner(mv, course):
    # course-u has a contract run but is in none of its programs; None is a
    # run whose course isn't in dim_course.
    mv.listed("u-1", course, order=None)
    unlisted = {"course_readable_id": course, **_PASSED}
    mv.insert(("uma", "Uma", "uma@x", True), "u-1", unlisted)
    mv.insert(("ann", "Ann Lee", "ann@x", True), "u-1", unlisted)
    learners, total = _run(mv)
    assert _shape(learners)["uma"] == []
    assert _shape(learners)["ann"] == ["run-1", "run-2", "run-3"]
    assert total == len(_ALL_ORDER) + 1
    passed, _ = _run(mv, completion_statuses=("passed",))
    assert "uma" not in _shape(passed)
    assert _counts(mv, search="uma") == (1, 0)


def test_not_enrolled_is_per_course(mv, course_x):
    course_x([("x-open", _RECENT)])
    # zed has course-x through x-open, so isn't missing it, whichever of its
    # runs or the course selects it.
    for filters in (
        {"course_readable_ids": ("course-x",)},
        {"courserun_readable_ids": ("x-soon",)},
    ):
        learners, _ = _run(mv, completion_statuses=_NOT_ENROLLED, **filters)
        assert "zed" not in _shape(learners)
        assert "ann" in _shape(learners)
    # With no course selected, zed misses course-1 to course-3.
    learners, _ = _run(mv, completion_statuses=_NOT_ENROLLED, search="zed")
    assert list(_shape(learners)) == ["zed"]
