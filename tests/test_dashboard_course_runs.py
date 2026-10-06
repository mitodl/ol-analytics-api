"""End-to-end tests for the b2b_dashboard course-runs endpoint.

Same harness as test_dashboard_learner_progress.py: drives the mounted app
over ASGITransport with an org manager's X-Userinfo header, stubbing only the
StarRocks pool and the MITx Online manager check.
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
from ol_analytics_api.tenants.b2b_dashboard.learner_models import CourseRun, CourseRunsResponse

ORG_ID = "11111111-1111-1111-1111-111111111111"
CONTRACT_ID = 101
PATH = f"/api/v1/analytics/organizations/{ORG_ID}/contracts/{CONTRACT_ID}/course-runs"
_AS_OF = datetime.datetime(2026, 9, 15, 6, 0)  # noqa: DTZ001 - StarRocks returns naive UTC


def _manager_header(organization_id=ORG_ID):
    claims = {"sub": "kc-uuid-1", "organization": {"an-alias": {"id": organization_id}}}
    return base64.b64encode(json.dumps(claims).encode()).decode()


def _row(**overrides):
    return {
        "courserun_id": "course-v1:MITxT+14.310x+2T2026",
        "courserun_title": "Data Analysis for Social Scientists",
        "courserun_start_on": "2026-02-01T00:00:00",
        "courserun_end_on": None,
        **overrides,
    }


class _FakePool:
    """Answers the as_of probe, the contract gate, the count query and the page
    query, recording every call. Course runs carry no PII, so there's no
    consent or status-count bucketing to fake, unlike learner-progress's pool."""

    def __init__(self, rows=(), total_count=0, *, contract_exists=True):
        self.rows = list(rows)
        self.total_count = total_count
        self.contract_exists = contract_exists
        self.calls = []

    async def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if query.startswith("SELECT 1 "):
            return [{"1": 1}] if self.contract_exists else []
        if "COUNT(*)" in query:
            return [{"total_count": self.total_count}]
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


async def test_envelope_lists_course_runs(app):
    pool = _FakePool(rows=[_row()], total_count=1)
    response = await _get(app, pool)

    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == ORG_ID
    assert body["as_of"] == "2026-09-15T06:00:00Z"
    assert body["total_count"] == 1
    [row] = body["data"]
    assert row["courserun_id"] == "course-v1:MITxT+14.310x+2T2026"
    assert row["courserun_end_on"] is None
    assert set(row) == set(CourseRun.model_fields)


async def test_scopes_to_org_and_contract_with_bound_values(app):
    pool = _FakePool()
    await _get(app, pool)
    page_query, page_params = pool.page_call()
    assert "sso_organization_id = %s AND contract_id = %s" in page_query
    assert "b2b_learner_records.mv_b2b_contract_courserun" in page_query
    assert page_params[:2] == (ORG_ID, CONTRACT_ID)
    count_query, count_params = pool.count_call()
    assert "sso_organization_id = %s AND contract_id = %s" in count_query
    assert count_params == page_params[:-2]


async def test_empty_contract_returns_no_rows(app):
    pool = _FakePool(rows=[], total_count=0)
    response = await _get(app, pool)
    body = response.json()
    assert body["total_count"] == 0
    assert body["data"] == []


async def test_nulls_start_date_sorts_last(app):
    pool = _FakePool()
    await _get(app, pool)
    assert pool.page_call()[0].endswith(
        "ORDER BY courserun_start_on IS NULL, courserun_start_on, courserun_title,"
        " courserun_readable_id"
        " LIMIT %s OFFSET %s"
    )


async def test_contract_not_in_org_is_403(app):
    pool = _FakePool(contract_exists=False)
    response = await _get(app, pool)
    assert response.status_code == 403
    assert not any("mv_b2b_contract_courserun" in query for query, _ in pool.calls)


async def test_non_manager_is_refused_before_any_query(app):
    pool = _FakePool()
    response = await _get(app, pool, is_manager=False)
    assert response.status_code == 403
    assert pool.calls == []


@pytest.mark.parametrize("model", [CourseRun, CourseRunsResponse])
def test_every_field_has_a_manager_facing_description(model):
    # Same rationale as learner-progress's own version of this test: the
    # dashboard can show these as help text to a manager, who never sees field
    # names, so every field needs one and none may lean on another field's name.
    field_names = set(CourseRun.model_fields) | set(CourseRunsResponse.model_fields)
    for name, field in model.model_fields.items():
        assert field.description, f"{name} has no description"
        named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", field.description)) & field_names
        assert not named, f"{name}'s description names {named}"
