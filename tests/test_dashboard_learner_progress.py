"""End-to-end tests for the b2b_dashboard learner-progress endpoint.

Drives the mounted app over ASGITransport with an org manager's X-Userinfo
header, stubbing only the StarRocks pool and the MITx Online manager check.
The SQL isn't executed; these tests pin what it scopes to, what it binds and
how consent shapes it.
"""

import base64
import datetime
import json
import re
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.core.db.refresh_metadata import _clear_cache
from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_dashboard import learner_models, learner_queries
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.learner_models import (
    CompletionStatusCounts,
    LearnerProgress,
    LearnerProgressResponse,
)

# What _outcomes_shared() renders with consent_fail_open off and on.
_CLOSED = "COALESCE(outcomes_decision, FALSE)"
_OPEN = "COALESCE(outcomes_decision, TRUE)"

ORG_ID = "11111111-1111-1111-1111-111111111111"
CONTRACT_ID = 101
PATH = f"/api/v1/analytics/organizations/{ORG_ID}/contracts/{CONTRACT_ID}/learner-progress"
_AS_OF = datetime.datetime(2026, 9, 15, 6, 0)  # noqa: DTZ001 - StarRocks returns naive UTC
# What the cluster answers UTC_TODAY_QUERY with: _AS_OF's date.
# Fixed, so the SQL these tests assert on is stable.
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
        "outcomes_shared": 0,
        "outcomes_consent_status": "not_recorded",
        "completion_status": None,
        "is_passing": None,
        "grade": None,
        "letter_grade": None,
        "content_progress": None,
        "certificate_issued_on": None,
        "certificate_is_revoked": None,
        "last_active_on": None,
        "days_active": None,
        "videos_watched": None,
        "problems_attempted": None,
        "chatbot_interactions": None,
        "needs_attention": None,
        **overrides,
    }


class _FakePool:
    """Answers the as_of probe, the contract gate, the count query and the page
    query, recording every call."""

    def __init__(
        self, rows=(), total_count=0, withheld=0, status_counts=None, *, contract_exists=True
    ):
        self.rows = list(rows)
        self.counts = {
            "total_count": total_count,
            "outcomes_withheld_count": withheld,
            "not_started": 0,
            "in_progress": 0,
            "passed": 0,
            "certified": 0,
            "needs_attention_count": 0,
            **(status_counts or {}),
        }
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
        if "COUNT(*)" in query:
            return [self.counts]
        return self.rows

    def page_call(self):
        return next(call for call in self.calls if call[0].endswith("LIMIT %s OFFSET %s"))

    def count_call(self):
        return next(call for call in self.calls if "COUNT(*)" in call[0])


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _clear_as_of_cache():
    _clear_cache()
    yield
    _clear_cache()


async def _get(app, pool, path=PATH, *, is_manager=True, params=None):
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=pool.fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=is_manager),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, params=params, headers={"X-Userinfo": _manager_header()})


async def test_envelope_withholds_outcomes_and_counts_them(app):
    pool = _FakePool(rows=[_row()], total_count=12, withheld=12)
    response = await _get(app, pool)

    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == ORG_ID
    # Zone-less UTC from StarRocks goes out with an offset, so a browser doesn't
    # read it as local time.
    assert body["as_of"] == "2026-09-15T06:00:00Z"
    assert body["total_count"] == 12
    assert body["outcomes_withheld_count"] == 12
    assert body["completion_status_counts"] == {
        "not_started": 0,
        "in_progress": 0,
        "passed": 0,
        "certified": 0,
    }
    [row] = body["data"]
    assert row["enrolled_on"] == "2026-02-03T14:22:11Z"
    assert row["email"] == "rgarcia@contoso.example"
    assert row["outcomes_shared"] is False
    assert row["completion_status"] is None
    assert set(row) == set(LearnerProgress.model_fields)


async def test_scopes_to_org_and_contract_with_bound_values(app):
    pool = _FakePool()
    await _get(app, pool)
    page_query, page_params = pool.page_call()
    assert "sso_organization_id = %s AND contract_id = %s" in page_query
    assert "b2b_learner_records.mv_b2b_learner_enrollment" in page_query
    assert page_params[:2] == (ORG_ID, CONTRACT_ID)
    count_query, count_params = pool.count_call()
    assert "sso_organization_id = %s AND contract_id = %s" in count_query
    assert count_params == page_params[:-2]


async def test_active_enrollments_only_unless_inactive_requested(app):
    pool = _FakePool()
    await _get(app, pool)
    assert "enrollment_is_active = TRUE" in pool.page_call()[0]

    pool = _FakePool()
    await _get(app, pool, params={"include_inactive": "true"})
    assert "enrollment_is_active = TRUE" not in pool.page_call()[0]


async def test_consent_fails_closed_by_default(app):
    assert type(settings)().consent_fail_open is False
    pool = _FakePool()
    await _get(app, pool)
    page_query, _ = pool.page_call()
    assert f"{_CLOSED} AS outcomes_shared" in page_query
    assert f"CASE WHEN {_CLOSED} THEN grade END AS grade" in page_query


async def test_consent_fail_open_discloses_outcomes(app, monkeypatch):
    monkeypatch.setattr(settings, "consent_fail_open", True)
    pool = _FakePool(rows=[_row(outcomes_shared=1, completion_status="passed", grade=0.8)])
    response = await _get(app, pool)

    [row] = response.json()["data"]
    assert (row["completion_status"], row["grade"]) == ("passed", 0.8)
    assert f"{_OPEN} AS outcomes_shared" in pool.page_call()[0]
    assert f"SUM(CASE WHEN {_OPEN} THEN 0 ELSE 1 END)" in pool.count_call()[0]


async def test_completion_status_counts_reported_from_the_count_query(app):
    status_counts = {"not_started": 2, "in_progress": 3, "passed": 1, "certified": 4}
    withheld = 1
    pool = _FakePool(
        # The buckets plus outcomes_withheld_count sum to total_count (11), the
        # invariant the endpoint promises; keep this fixture consistent with it.
        total_count=sum(status_counts.values()) + withheld,
        withheld=withheld,
        status_counts=status_counts,
    )
    response = await _get(app, pool)

    body = response.json()
    assert body["completion_status_counts"] == status_counts
    assert sum(status_counts.values()) + body["outcomes_withheld_count"] == body["total_count"]


async def test_completion_status_counts_share_the_response_filters(app):
    pool = _FakePool()
    await _get(app, pool, params={"completion_status": ["passed"]})
    count_query, _ = pool.count_call()
    # Buckets come off the same WHERE clause as total_count, so they narrow
    # along with the rest of the envelope rather than staying contract-wide.
    assert count_query.count("WHERE") == 2
    for status in ("not_started", "in_progress", "passed", "certified"):
        assert (
            f"SUM(CASE WHEN {_CLOSED} AND completion_status = '{status}' THEN 1 ELSE 0 END)"
            f" AS {status}" in count_query
        )


async def test_completion_status_is_read_from_the_view_not_derived(app):
    # The rule lives in ol-data-platform's afact_learner_courserun_progress,
    # which the B2B views count from too. Restating it here is how the two
    # drifted before.
    pool = _FakePool()
    await _get(app, pool)
    page_query = pool.page_call()[0]
    assert " completion_status, is_passing," in page_query
    assert "certificate_is_revoked = FALSE" not in page_query


async def test_needs_attention_count_reported_from_the_count_query(app):
    pool = _FakePool(status_counts={"needs_attention_count": 7})
    response = await _get(app, pool)

    assert response.json()["needs_attention_count"] == 7
    count_query, _ = pool.count_call()
    # Built from the production expression rather than a copy of it: what this
    # pins is the aggregate's shape and its consent gate, not the rule inside.
    # The rule is pinned by the sqlite tests below, which execute it.
    assert (
        f"SUM(CASE WHEN {_CLOSED} AND ({_needs_attention_sql(_TODAY)})"
        " THEN 1 ELSE 0 END) AS needs_attention_count" in count_query
    )


async def test_needs_attention_count_shares_the_response_filters(app):
    pool = _FakePool()
    await _get(app, pool, params={"completion_status": ["passed"]})
    count_query, _ = pool.count_call()
    # Same query, same WHERE clause as total_count and the status buckets.
    assert count_query.count("WHERE") == 2
    assert "needs_attention_count" in count_query


def _needs_attention_sql(today):
    # The production string, verbatim -- no substitution. Today's date is
    # resolved on the cluster and spliced as a date literal, which sqlite
    # parses too, so these tests execute exactly what StarRocks would.
    return learner_queries._needs_attention(today)  # noqa: SLF001


def _enrollment_db(columns, rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE enrollment ({', '.join(columns)})")
    conn.executemany(
        f"INSERT INTO enrollment VALUES ({', '.join('?' * len(columns))})",  # noqa: S608
        rows,
    )
    return conn


def test_needs_attention_boundary_is_computed_from_real_rows():
    # test_needs_attention_count_reported_from_the_count_query pins the SQL
    # text; this actually runs ._needs_attention against rows in sqlite, so a
    # day-30 regression (or a reverted `<=`) fails here even though _FakePool
    # never evaluates a WHERE clause on its own. Which statuses get a date, and
    # that it is last activity plus 30 days, is pinned where the date is made:
    # ol-data-platform's unit test on afact_learner_courserun_progress.
    needs_attention = _needs_attention_sql(_TODAY)
    conn = _enrollment_db(
        ("completion_status TEXT", "needs_attention_since TEXT"),
        [
            ("not_started", "2026-02-03"),  # never started: dated from the enrollment
            ("in_progress", (_TODAY + datetime.timedelta(days=1)).isoformat()),  # 29 days quiet
            ("in_progress", _TODAY.isoformat()),  # quiet exactly 30 days
            ("in_progress", None),  # graded, but no tracked activity at all
            ("certified", None),  # finished: the view never dates these
        ],
    )
    rows = conn.execute(
        f"SELECT completion_status, ({needs_attention}) AS needs_attention FROM enrollment"  # noqa: S608
    ).fetchall()
    conn.close()

    assert rows == [
        ("not_started", 1),
        ("in_progress", 0),  # still recent
        ("in_progress", 1),  # the 30th day is included
        # No date to compare, so: no. Not NULL -- see
        # test_needs_attention_is_two_valued_so_the_filter_partitions_rows.
        ("in_progress", 0),
        ("certified", 0),
    ]


def test_needs_attention_count_respects_the_consent_gate(monkeypatch):
    # Runs the full SUM(CASE WHEN shared AND (...)) aggregate over rows that
    # all need attention and differ only in the recorded consent decision. A
    # recorded decision wins either way; consent_fail_open decides the rest.
    needs_attention = _needs_attention_sql(_TODAY)
    due = (_TODAY - datetime.timedelta(days=1)).isoformat()
    conn = _enrollment_db(
        ("needs_attention_since TEXT", "outcomes_decision INTEGER"),
        # two undecided, one consented, one declined
        [(due, None), (due, None), (due, 1), (due, 0)],
    )

    def count():
        shared = learner_queries._outcomes_shared()  # noqa: SLF001
        query = (
            f"SELECT SUM(CASE WHEN {shared} AND ({needs_attention}) THEN 1 ELSE 0 END),"  # noqa: S608
            f" SUM(CASE WHEN {shared} THEN 0 ELSE 1 END) FROM enrollment"
        )
        return conn.execute(query).fetchone()

    assert type(settings)().consent_fail_open is False
    assert count() == (1, 3)  # only the recorded consent is disclosed

    monkeypatch.setattr(settings, "consent_fail_open", True)
    assert count() == (3, 1)  # the recorded decline stays withheld
    conn.close()


def test_needs_attention_is_two_valued_so_the_filter_partitions_rows():
    # The view leaves needs_attention_since NULL for passed and certified rows
    # and for an in_progress row with a grade but no tracked activity, which
    # makes the comparison NULL. Unguarded that is neither true nor false, so
    # the row would fall out of `needs_attention=true` and
    # `needs_attention=false` alike while still counting toward total_count,
    # and would serialize as `needs_attention: null` on a row whose outcomes
    # are shared. COALESCE in ._needs_attention settles it as false; this pins
    # that every shared row lands in exactly one direction of the filter.
    needs_attention = _needs_attention_sql(_TODAY)
    conn = _enrollment_db(
        ("needs_attention_since TEXT",),
        [
            ("2026-02-03",),  # never started
            (None,),  # graded, but no tracked activity at all
            ((_TODAY + datetime.timedelta(days=1)).isoformat(),),  # active 29 days ago
            (_TODAY.isoformat(),),  # active exactly 30 days ago
            (None,),  # certified
            (None,),  # passed
        ],
    )

    def matching(*, direction):
        negate = "" if direction else "NOT "
        return conn.execute(
            f"SELECT COUNT(*) FROM enrollment WHERE (TRUE AND {negate}({needs_attention}))"  # noqa: S608
        ).fetchone()[0]

    counted = conn.execute(
        f"SELECT SUM(CASE WHEN TRUE AND ({needs_attention}) THEN 1 ELSE 0 END) FROM enrollment"  # noqa: S608
    ).fetchone()[0]
    nulls = conn.execute(
        f"SELECT COUNT(*) FROM enrollment WHERE ({needs_attention}) IS NULL"  # noqa: S608
    ).fetchone()[0]
    selected = (matching(direction=True), matching(direction=False))
    conn.close()

    assert nulls == 0
    # The two directions partition all six rows: none lost, none double-counted.
    assert selected == (2, 4)
    # And `true` selects exactly the rows needs_attention_count counts.
    assert selected[0] == counted


async def test_needs_attention_filter_is_consent_gated_in_both_directions(app):
    for value, negate in (("true", ""), ("false", "NOT ")):
        pool = _FakePool()
        await _get(app, pool, params={"needs_attention": value})
        # From the production expression, not a copy: this pins the gate and
        # the negation, and the sqlite tests pin the rule they wrap.
        expected = f"({_CLOSED} AND {negate}({_needs_attention_sql(_TODAY)}))"
        # The COALESCE is the consent gate: a withheld row matches neither
        # direction, so the filter can't reveal the outcome it withholds.
        assert expected in pool.page_call()[0]
        assert expected in pool.count_call()[0]


async def test_needs_attention_filter_is_omitted_when_not_given(app):
    # Absent means "don't filter", which is the only way to see withheld rows.
    # The row projection still carries the expression; only the predicate goes.
    pool = _FakePool()
    await _get(app, pool)
    page_query = pool.page_call()[0]
    assert "FALSE AND (COALESCE" not in page_query
    assert "FALSE AND NOT (COALESCE" not in page_query
    assert page_query.count("WHERE") == 1


async def test_needs_attention_filter_narrows_the_counts_with_the_page(app):
    # Same WHERE on both, so needs_attention_count and the status buckets
    # describe the filtered set the page came from, not the contract.
    pool = _FakePool()
    await _get(app, pool, params={"needs_attention": "true", "completion_status": ["in_progress"]})
    count_query, _ = pool.count_call()
    assert count_query.count("WHERE") == 2
    assert "completion_status IN (%s)" in count_query
    assert "needs_attention_count" in count_query


async def test_needs_attention_row_field_reuses_the_count_expression(app):
    # One expression behind the row, the filter and the count, so mit-learn
    # never has to recompute the 30-day rule against a browser's own "today".
    pool = _FakePool()
    await _get(app, pool)
    page_query = pool.page_call()[0]
    assert (
        f"CASE WHEN {_CLOSED} THEN {learner_queries._needs_attention(_TODAY)} END"  # noqa: SLF001
        " AS needs_attention" in page_query
    )
    assert learner_queries._needs_attention(_TODAY) in pool.count_call()[0]  # noqa: SLF001


async def test_today_is_resolved_once_on_the_cluster_and_shared_by_both_statements(app):
    # The page and the count are two round trips. With the date left in the
    # SQL each would evaluate it separately, so a request straddling midnight
    # would filter the page on one date and count on another -- an off-by-one
    # between a row's needs_attention and needs_attention_count, and a
    # total_count the page contradicts.
    pool = _FakePool()
    await _get(app, pool, params={"needs_attention": "true"})

    queries = [query for query, _ in pool.calls]
    assert queries.count(learner_queries.UTC_TODAY_QUERY) == 1
    # Resolved before either statement is built, like as_of.
    today_at = queries.index(learner_queries.UTC_TODAY_QUERY)
    assert today_at < queries.index(pool.page_call()[0])
    assert today_at < queries.index(pool.count_call()[0])

    for query in (pool.page_call()[0], pool.count_call()[0]):
        assert "CURRENT_DATE" not in query
        assert _TODAY_SQL in query


def test_today_query_reads_no_table_and_asks_for_utc():
    # No FROM clause, so StarRocks answers it without touching storage. That
    # is what makes the extra round trip per request cheap enough to prefer
    # over caching a value whose whole purpose is to be correct at a boundary.
    # needs_attention_since is defined against the UTC date, and CURRENT_DATE()
    # would follow the session's timezone instead.
    assert " FROM " not in learner_queries.UTC_TODAY_QUERY
    assert "UTC_TIMESTAMP()" in learner_queries.UTC_TODAY_QUERY
    assert "CURRENT_DATE" not in learner_queries.UTC_TODAY_QUERY


@pytest.mark.parametrize(
    "value",
    [
        _TODAY,
        # Which of these a DATE column arrives as depends on the driver and
        # the StarRocks build, so all three normalize to the same literal
        # rather than 500ing the endpoint on a type surprise.
        datetime.datetime(2026, 9, 15, 9, 30),  # noqa: DTZ001 - StarRocks returns naive
        "2026-09-15",
    ],
)
def test_date_literal_normalizes_every_shape_the_driver_can_return(value):
    # datetime is the one that matters: it subclasses date, so rendering its
    # isoformat unchanged would emit a time component and change the
    # comparison. It is truncated, not passed through.
    assert learner_queries._date_literal(value) == _TODAY_SQL  # noqa: SLF001


@pytest.mark.parametrize(
    ("value", "error"),
    [(None, TypeError), (20260816, TypeError), ("16/08/2026", ValueError)],
)
def test_date_literal_refuses_what_it_cannot_read_as_a_date(value, error):
    # Today's date is spliced, not bound, so nothing that isn't a real calendar
    # date may reach the query text.
    with pytest.raises(error):
        learner_queries._date_literal(value)  # noqa: SLF001


async def test_needs_attention_row_field_is_served_and_withheld_with_consent(app):
    pool = _FakePool(
        rows=[_row(outcomes_shared=1, completion_status="not_started", needs_attention=1)]
    )
    assert (await _get(app, pool)).json()["data"][0]["needs_attention"] is True

    pool = _FakePool(rows=[_row(outcomes_shared=0, needs_attention=1)])
    assert (await _get(app, pool)).json()["data"][0]["needs_attention"] is None


async def test_non_boolean_needs_attention_is_rejected(app):
    response = await _get(app, _FakePool(), params={"needs_attention": "stale"})
    assert response.status_code == 422


def test_completion_status_buckets_are_mutually_exclusive_and_exhaustive():
    # Each row's completion_status is exactly one of the view's four values
    # (an accepted_values test in ol-data-platform), so the four buckets never
    # overlap and, with outcomes_withheld_count, always sum to total_count.
    assert learner_queries._STATUSES == ("not_started", "in_progress", "passed", "certified")  # noqa: SLF001


async def test_search_is_bound_with_wildcards_escaped(app):
    pool = _FakePool()
    await _get(app, pool, params={"search": "Garcia_50%"})
    page_query, page_params = pool.page_call()
    assert "(LOWER(email) LIKE %s OR LOWER(full_name) LIKE %s)" in page_query
    assert "garcia" not in page_query
    assert page_params[-4:-2] == ("%garcia\\_50\\%%", "%garcia\\_50\\%%")


async def test_courserun_filter_is_bound_when_given_and_omitted_otherwise(app):
    pool = _FakePool()
    await _get(app, pool, params={"courserun_readable_id": "course-v1:MITxT+14.310x+2T2026"})
    page_query, page_params = pool.page_call()
    count_query, count_params = pool.count_call()
    assert "courserun_readable_id = %s" in page_query
    assert "courserun_readable_id = %s" in count_query
    assert "course-v1:MITxT+14.310x+2T2026" in page_params
    assert "course-v1:MITxT+14.310x+2T2026" in count_params

    pool = _FakePool()
    await _get(app, pool)
    assert "courserun_readable_id = %s" not in pool.page_call()[0]


async def test_empty_courserun_filter_is_rejected(app):
    # An explicit empty value is a malformed request, not "no filter" -- silently
    # falling back to the unfiltered list would hide the mistake.
    response = await _get(app, _FakePool(), params={"courserun_readable_id": ""})
    assert response.status_code == 422


async def test_status_filter_cannot_reveal_withheld_statuses(app):
    pool = _FakePool()
    await _get(app, pool, params={"completion_status": ["passed", "unknown"]})
    page_query, page_params = pool.page_call()
    assert f"(({_CLOSED} AND completion_status IN (%s)) OR NOT {_CLOSED})" in page_query
    assert "passed" in page_params


async def test_sort_puts_nulls_last_with_a_unique_tie_break(app):
    pool = _FakePool()
    await _get(app, pool, params={"sort": "email", "descending": "true"})
    assert pool.page_call()[0].endswith(
        "ORDER BY email IS NULL, email DESC, user_pk, courserun_pk LIMIT %s OFFSET %s"
    )


async def test_names_are_read_from_the_view_as_published(app):
    # mv_b2b_learner_enrollment nulls blank names, so they sort with the
    # missing ones without this query restating the rule.
    pool = _FakePool()
    await _get(app, pool)
    query = pool.page_call()[0]
    assert " email, full_name, courserun_readable_id," in query
    assert "TRIM(" not in query


async def test_unknown_sort_key_is_rejected(app):
    response = await _get(app, _FakePool(), params={"sort": "grade"})
    assert response.status_code == 422


async def test_contract_not_in_org_is_403(app):
    pool = _FakePool(contract_exists=False)
    response = await _get(app, pool)
    assert response.status_code == 403
    assert not any("mv_b2b_learner_enrollment" in query for query, _ in pool.calls)


async def test_non_manager_is_refused_before_any_query(app):
    pool = _FakePool()
    response = await _get(app, pool, is_manager=False)
    assert response.status_code == 403
    assert pool.calls == []


def test_models_null_outcomes_a_row_carries_when_not_shared():
    progress = LearnerProgress(
        **_row(completion_status="certified", is_passing=1, grade=0.91, videos_watched=7)
    )
    assert (progress.completion_status, progress.is_passing, progress.grade) == (None, None, None)
    assert progress.videos_watched is None


def test_every_outcome_column_is_consent_gated_in_the_query():
    query = learner_queries.learner_progress(
        learner_queries.ProgressFilters(organization_id=ORG_ID, contract_id=CONTRACT_ID), _TODAY
    )
    for name in (
        "completion_status",
        "is_passing",
        "grade",
        "letter_grade",
        "content_progress",
        "last_active_on",
        "days_active",
        "videos_watched",
        "problems_attempted",
        "chatbot_interactions",
    ):
        assert f"CASE WHEN {_CLOSED} THEN {name} END AS {name}" in query.page
    # needs_attention is derived, not selected, so the same gate wraps an
    # expression rather than a column name.
    assert (
        f"CASE WHEN {_CLOSED} THEN {learner_queries._needs_attention(_TODAY)} END"  # noqa: SLF001
        " AS needs_attention" in query.page
    )


def test_the_model_gates_every_outcome_the_query_projects():
    # The two lists are maintained apart (learner_models._OUTCOME_FIELDS is the
    # second check the query change can't defeat), so pin them in step: an
    # outcome added to the query alone would reach a manager ungated.
    assert set(learner_models._OUTCOME_FIELDS) == {  # noqa: SLF001
        *learner_queries._OUTCOMES,  # noqa: SLF001
        "needs_attention",
    }


@pytest.mark.parametrize(
    "model", [LearnerProgress, LearnerProgressResponse, CompletionStatusCounts]
)
def test_every_field_has_a_manager_facing_description(model):
    # The dashboard can show these as help text to a manager, who never sees
    # field names, so every field needs one and none may lean on another field's
    # name.
    field_names = (
        set(LearnerProgress.model_fields)
        | set(LearnerProgressResponse.model_fields)
        | set(CompletionStatusCounts.model_fields)
    )
    for name, field in model.model_fields.items():
        assert field.description, f"{name} has no description"
        named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", field.description)) & field_names
        assert not named, f"{name}'s description names {named}"


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
    "days_active",
    "videos_played",
    "problems_attempted",
    "chatbot_interactions",
    "needs_attention_since",
    "outcomes_shared",
)


@pytest.mark.parametrize(
    ("fail_open", "disclosed"),
    [(False, {"consented"}), (True, {"consented", "undecided"})],
)
def test_recorded_decision_is_read_from_the_view(monkeypatch, fail_open, disclosed):
    # Runs learner_progress() itself against an MV-shaped table, so the inner
    # select has to carry the view's outcomes_shared through: three learners
    # who all passed and differ only in the decision recorded for the contract.
    monkeypatch.setattr(settings, "consent_fail_open", fail_open)
    schema = settings.learner_records_schema
    table = f"{schema}.{learner_queries.ENROLLMENT_MV}"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH ':memory:' AS {schema}")
    conn.execute(f"CREATE TABLE {table} ({', '.join(_ENROLLMENT_MV_COLUMNS)})")
    for name, decision in (("consented", True), ("declined", False), ("undecided", None)):
        row = {
            "user_pk": name,
            "courserun_pk": "run",
            "user_global_id": name,
            "sso_organization_id": ORG_ID,
            "contract_id": CONTRACT_ID,
            "enrollment_is_active": True,
            "completion_status": "passed",
            "is_passing": True,
            "grade_value": 0.8,
            "days_active": 3,
            "videos_played": 7,
            "problems_attempted": 5,
            "chatbot_interactions": 2,
            "outcomes_shared": decision,
        }
        conn.execute(
            f"INSERT INTO {table} VALUES ({', '.join('?' * len(_ENROLLMENT_MV_COLUMNS))})",  # noqa: S608
            [row.get(column) for column in _ENROLLMENT_MV_COLUMNS],
        )

    def run(**filters):
        query = learner_queries.learner_progress(
            learner_queries.ProgressFilters(
                organization_id=ORG_ID, contract_id=CONTRACT_ID, **filters
            ),
            _TODAY,
        )
        rows = conn.execute(query.page.replace("%s", "?"), (*query.params, 100, 0)).fetchall()
        count = conn.execute(query.count.replace("%s", "?"), query.params).fetchone()
        return {row["learner_id"]: row for row in rows}, count

    rows, count = run()
    assert {name for name, row in rows.items() if row["grade"] is not None} == disclosed
    assert {name for name, row in rows.items() if row["needs_attention"] is not None} == disclosed
    activity = ("days_active", "videos_watched", "problems_attempted", "chatbot_interactions")
    assert {name: tuple(row[column] for column in activity) for name, row in rows.items()} == {
        name: (3, 7, 5, 2) if name in disclosed else (None,) * 4 for name in rows
    }
    assert count["outcomes_withheld_count"] == 3 - len(disclosed)
    assert count["passed"] == len(disclosed)
    # The recorded decision itself is reported whatever the default resolved to.
    assert {name: row["outcomes_consent_status"] for name, row in rows.items()} == {
        "consented": "consented",
        "declined": "declined",
        "undecided": "not_recorded",
    }

    # Neither filter may say what a withheld row is withholding.
    assert set(run(completion_statuses=("passed",))[0]) == disclosed
    assert set(run(completion_statuses=("unknown",))[0]) == set(rows) - disclosed
    assert set(run(needs_attention=False)[0]) == disclosed
    conn.close()
