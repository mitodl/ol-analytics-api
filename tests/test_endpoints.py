"""End-to-end tests for the b2b_dashboard analytics endpoints.

Drives the real mounted app over ASGITransport with a valid X-Userinfo
header, so the tenant's auth dependencies actually run. The StarRocks pool
and the MITx Online round-trip are the only things stubbed — everything
between the HTTP request and the response envelope (auth gate, suppression,
as_of lookup, envelope shape) is exercised for real.
"""

import base64
import datetime
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.core.db.client import PoolAcquireTimeoutError
from ol_analytics_api.core.db.refresh_metadata import _clear_cache
from ol_analytics_api.main import create_app

_AS_OF = datetime.datetime(2026, 7, 2, 4, 0, 0)  # noqa: DTZ001


def _userinfo_header(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _manager_header(org_slug: str) -> str:
    return _userinfo_header({"sub": "kc-uuid-1", "organization": {org_slug: {"id": "o1"}}})


def _admin_header() -> str:
    return _userinfo_header({"sub": "kc-uuid-2", "realm_access": {"roles": ["mit_contract_admin"]}})


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _clear_as_of_cache():
    # The as_of value is cached process-globally; start every test from empty
    # so one test's stubbed timestamp can't leak into the next.
    _clear_cache()
    yield
    _clear_cache()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _fake_fetch_all(data_rows):
    """A fetch_all stub that answers the information_schema as_of probe with
    _AS_OF and every other query with the given MV rows."""

    async def fetch_all(query, *_args):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        return list(data_rows)

    return fetch_all


async def test_org_endpoint_returns_envelope_and_suppresses_small_cohorts(app):
    # Two rows above the floor of 5, one below (seats_consumed=3) that must
    # be withheld from the response entirely.
    rows = [
        {
            "organization_key": "org-a",
            "organization_name": "Org A",
            "contract_pk": 1,
            "b2b_contract_name": "C1",
            "b2b_contract_is_active": True,
            "b2b_contract_start_date": None,
            "b2b_contract_end_date": None,
            "seat_limit": 100,
            "b2b_contract_membership_type": "seat",
            "seats_consumed": 40,
            "active_learners": 30,
            "learners_certified": 10,
            "seat_utilization_pct": 40.0,
            "completion_rate_pct": 25.0,
        },
        {**_row_template(), "contract_pk": 2, "seats_consumed": 3},
    ]
    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_fake_fetch_all(rows),
        ),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/org-a/contract-utilization",
                headers={"X-Userinfo": _manager_header("org-a")},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["organization_key"] == "org-a"
    assert body["as_of"].startswith("2026-07-02T04:00:00")
    # The sub-floor contract_pk=2 row is suppressed; only contract_pk=1 remains.
    assert [row["contract_pk"] for row in body["data"]] == [1]


async def test_org_endpoint_403_for_member_who_is_not_a_manager(app):
    # A member (org present in the claim) whose MITx Online manager check
    # comes back False is rejected — membership alone isn't enough.
    with (
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_fake_fetch_all([]),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/org-a/contract-utilization",
                headers={"X-Userinfo": _manager_header("org-a")},
            )
    assert response.status_code == 403


async def test_org_endpoint_authorized_but_empty_returns_empty_data_not_404(app):
    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_fake_fetch_all([]),
        ),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/org-a/enrollment-funnel",
                headers={"X-Userinfo": _manager_header("org-a")},
            )
    assert response.status_code == 200
    body = response.json()
    assert body["organization_key"] == "org-a"
    assert body["data"] == []
    assert body["as_of"].startswith("2026-07-02T04:00:00")


async def test_admin_endpoint_envelope_has_no_organization_key(app):
    row = {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "contract_pk": 1,
        "b2b_contract_name": "C1",
        "b2b_contract_is_active": True,
        "b2b_contract_start_date": None,
        "b2b_contract_end_date": None,
        "seat_limit": 100,
        "b2b_contract_membership_type": "seat",
        "seats_consumed": 40,
        "active_learners": 30,
        "certified_learners": 10,
        "seat_utilization_pct": 40.0,
        "completion_rate_pct": 25.0,
        "health_status": "healthy",
    }
    with patch(
        "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
        new=_fake_fetch_all([row]),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/admin/contract-health",
                headers={"X-Userinfo": _admin_header()},
            )
    assert response.status_code == 200
    body = response.json()
    assert "organization_key" not in body
    assert body["as_of"].startswith("2026-07-02T04:00:00")
    assert len(body["data"]) == 1


async def test_admin_endpoint_403_without_realm_role(app):
    async with _client(app) as client:
        response = await client.get(
            "/api/v1/analytics/admin/contract-health",
            headers={"X-Userinfo": _manager_header("org-a")},  # no admin realm role
        )
    assert response.status_code == 403


async def test_as_of_is_null_when_no_mv_has_refreshed(app):
    async def fetch_all(query, *_args):
        if "information_schema" in query:
            return [{"as_of": None}]
        return []

    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/org-a/program-funnel",
                headers={"X-Userinfo": _manager_header("org-a")},
            )
    assert response.status_code == 200
    assert response.json()["as_of"] is None


async def test_org_endpoint_applies_default_pagination_to_query(app):
    # A caller passing no page params still gets a bounded query — the whole
    # point of the DoS fix: an unbounded grain can't be pulled whole.
    captured = {}

    async def fetch_all(query, params):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        captured["query"] = query
        captured["params"] = params
        return []

    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/org-a/content-engagement",
                headers={"X-Userinfo": _manager_header("org-a")},
            )
    assert response.status_code == 200
    assert "LIMIT %s OFFSET %s" in captured["query"]
    assert "ORDER BY" in captured["query"]
    # (org_slug, limit=default 100, offset=0)
    assert captured["params"] == ("org-a", 100, 0)


async def test_org_endpoint_honors_limit_and_offset(app):
    captured = {}

    async def fetch_all(query, params):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        captured["params"] = params
        return []

    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/org-a/enrollment-funnel?limit=25&offset=50",
                headers={"X-Userinfo": _manager_header("org-a")},
            )
    assert response.status_code == 200
    assert captured["params"] == ("org-a", 25, 50)


async def test_org_endpoint_rejects_out_of_range_limit(app):
    # limit above max_page_size (1000) is a 422 before any query runs.
    with patch(
        "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
        new=AsyncMock(return_value=True),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/org-a/content-engagement?limit=5000",
                headers={"X-Userinfo": _manager_header("org-a")},
            )
    assert response.status_code == 422


async def test_admin_endpoint_applies_pagination(app):
    captured = {}

    async def fetch_all(query, params):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        captured["query"] = query
        captured["params"] = params
        return []

    with patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=fetch_all):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/admin/contract-health?limit=10",
                headers={"X-Userinfo": _admin_header()},
            )
    assert response.status_code == 200
    assert "LIMIT %s OFFSET %s" in captured["query"]
    assert captured["params"] == (10, 0)


async def test_pool_saturation_returns_503(app):
    # A saturated shared pool must fail fast as 503 for a tenant request, not
    # bubble up as a 500.
    async def fetch_all(query, _params):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        msg = "pool saturated"
        raise PoolAcquireTimeoutError(msg)

    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/org-a/contract-utilization",
                headers={"X-Userinfo": _manager_header("org-a")},
            )
    assert response.status_code == 503


def _row_template() -> dict:
    return {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "contract_pk": 1,
        "b2b_contract_name": "C1",
        "b2b_contract_is_active": True,
        "b2b_contract_start_date": None,
        "b2b_contract_end_date": None,
        "seat_limit": 100,
        "b2b_contract_membership_type": "seat",
        "seats_consumed": 40,
        "active_learners": 30,
        "learners_certified": 10,
        "seat_utilization_pct": 40.0,
        "completion_rate_pct": 25.0,
    }
