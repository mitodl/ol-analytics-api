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
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.core.db.refresh_metadata import _clear_cache
from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_dashboard import learner_queries
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.learner_models import (
    CompletionStatusCounts,
    LearnerProgress,
    LearnerProgressResponse,
)

ORG_ID = "11111111-1111-1111-1111-111111111111"
CONTRACT_ID = 101
PATH = f"/api/v1/analytics/organizations/{ORG_ID}/contracts/{CONTRACT_ID}/learner-progress"
_AS_OF = datetime.datetime(2026, 9, 15, 6, 0)  # noqa: DTZ001 - StarRocks returns naive UTC


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
        "completion_status": None,
        "is_passing": None,
        "grade": None,
        "letter_grade": None,
        "certificate_issued_on": None,
        "certificate_is_revoked": None,
        "last_active_on": None,
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
            **(status_counts or {}),
        }
        self.contract_exists = contract_exists
        self.calls = []

    async def fetch_all(self, query, params=()):
        self.calls.append((query, params))
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
    assert "FALSE AS outcomes_shared" in page_query
    assert "CASE WHEN FALSE THEN grade END AS grade" in page_query


async def test_consent_fail_open_discloses_outcomes(app, monkeypatch):
    monkeypatch.setattr(settings, "consent_fail_open", True)
    pool = _FakePool(rows=[_row(outcomes_shared=1, completion_status="passed", grade=0.8)])
    response = await _get(app, pool)

    [row] = response.json()["data"]
    assert (row["completion_status"], row["grade"]) == ("passed", 0.8)
    assert "TRUE AS outcomes_shared" in pool.page_call()[0]
    assert "SUM(CASE WHEN TRUE THEN 0 ELSE 1 END)" in pool.count_call()[0]


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
            f"SUM(CASE WHEN FALSE AND completion_status = '{status}' THEN 1 ELSE 0 END)"
            f" AS {status}" in count_query
        )


def test_completion_status_buckets_are_mutually_exclusive_and_exhaustive():
    # Each row's completion_status is exactly one CASE branch
    # (learner_queries._COMPLETION_STATUS), so the four buckets never overlap
    # and, with outcomes_withheld_count, always sum to total_count.
    assert learner_queries._STATUSES == ("not_started", "in_progress", "passed", "certified")  # noqa: SLF001


async def test_search_is_bound_with_wildcards_escaped(app):
    pool = _FakePool()
    await _get(app, pool, params={"search": "Garcia_50%"})
    page_query, page_params = pool.page_call()
    assert "(LOWER(email) LIKE %s OR LOWER(full_name) LIKE %s)" in page_query
    assert "garcia" not in page_query
    assert page_params[-4:-2] == ("%garcia\\_50\\%%", "%garcia\\_50\\%%")


async def test_status_filter_cannot_reveal_withheld_statuses(app):
    pool = _FakePool()
    await _get(app, pool, params={"completion_status": ["passed", "unknown"]})
    page_query, page_params = pool.page_call()
    assert "((FALSE AND completion_status IN (%s)) OR NOT FALSE)" in page_query
    assert "passed" in page_params


async def test_sort_puts_nulls_last_with_a_unique_tie_break(app):
    pool = _FakePool()
    await _get(app, pool, params={"sort": "email", "descending": "true"})
    assert pool.page_call()[0].endswith(
        "ORDER BY email IS NULL, email DESC, user_pk, courserun_pk LIMIT %s OFFSET %s"
    )


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
    progress = LearnerProgress(**_row(completion_status="certified", is_passing=1, grade=0.91))
    assert (progress.completion_status, progress.is_passing, progress.grade) == (None, None, None)


def test_every_outcome_column_is_consent_gated_in_the_query():
    query = learner_queries.learner_progress(
        learner_queries.ProgressFilters(organization_id=ORG_ID, contract_id=CONTRACT_ID)
    )
    for name in ("completion_status", "is_passing", "grade", "letter_grade"):
        assert f"CASE WHEN FALSE THEN {name} END AS {name}" in query.page


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
