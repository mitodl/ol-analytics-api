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


# The Keycloak org UUID (sso_organization_id) that require_org_manager matches on.
ORG_A_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"


def _manager_header(organization_id: str) -> str:
    # The `organization` claim is keyed by org alias; the org UUID rides in the
    # value's `id` (Keycloak addOrganizationId mapper), which is what auth matches.
    return _userinfo_header(
        {"sub": "kc-uuid-1", "organization": {"an-alias": {"id": organization_id}}}
    )


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


def _is_count_query(query):
    return "COUNT(*)" in query


def _fake_fetch_all(data_rows, total_count=0):
    """A fetch_all stub that answers the information_schema as_of probe with
    _AS_OF, the envelope's total-count query with ``total_count``, and every
    other query with the given MV rows."""

    async def fetch_all(query, *_args):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if _is_count_query(query):
            return [{"total_count": total_count}]
        return list(data_rows)

    return fetch_all


def _capture_page_query(captured, rows=(), total_count=0):
    """A fetch_all stub that records the *paged* SELECT specifically. The as_of
    probe and the total-count query go through the same pool, so a stub that
    captured every query would report whichever happened to run last."""

    async def fetch_all(query, params):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if _is_count_query(query):
            return [{"total_count": total_count}]
        captured["query"] = query
        captured["params"] = params
        return list(rows)

    return fetch_all


async def test_org_endpoint_returns_envelope_and_suppresses_small_cohorts(app):
    # Two rows above the floor of 5, one below (seats_consumed=3) that must
    # be withheld from the response entirely.
    rows = [
        {
            "organization_key": "org-a",
            "organization_name": "Org A",
            "contract_pk": "contract-pk-1",
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
        {**_row_template(), "contract_pk": "contract-pk-2", "seats_consumed": 3},
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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contract-utilization",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == ORG_A_ID
    assert body["as_of"].startswith("2026-07-02T04:00:00")
    # The sub-floor contract_pk="contract-pk-2" row is suppressed; only
    # contract_pk="contract-pk-1" remains.
    assert [row["contract_pk"] for row in body["data"]] == ["contract-pk-1"]


async def test_org_envelope_carries_the_total_row_count(app):
    """Without this a client cannot tell a full page from a truncated one: a
    response of exactly `limit` rows looks identical either way."""
    rows = [{**_row_template(), "contract_pk": pk} for pk in ("contract-pk-1", "contract-pk-2")]
    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_fake_fetch_all(rows, total_count=340),
        ),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contract-utilization?limit=2",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    body = response.json()
    # The total is the whole result set, not this page — that difference is the
    # entire signal the client needs to offer "load the rest".
    assert body["total_count"] == 340
    assert len(body["data"]) == 2


async def test_total_count_query_applies_the_anonymization_floor(app):
    """The count must be gated on the primary cohort like the rows are. A raw
    COUNT(*) would exceed anything paging can reach, and the difference would
    tell the caller precisely how many sub-floor cohorts their org has."""
    captured = {}

    async def fetch_all(query, params):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if _is_count_query(query):
            captured["query"] = query
            captured["params"] = params
            return [{"total_count": 7}]
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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contract-utilization",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    assert "seats_consumed >= %s" in captured["query"]
    # (organization_id, floor) — both bound, never spliced.
    assert captured["params"] == (ORG_A_ID, 5)


async def test_content_engagement_suppresses_secondary_counts_and_derivatives(app):
    # 50 enrolled clears the primary floor, so the row survives — but its
    # secondary counts and their derivatives must be nulled: chatbot_users=2
    # and certificates_earned=1 name too few learners, and the video totals
    # and averages are computed over a single engaged learner.
    row = {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "courserun_readable_id": "course-v1:MITx+6.00+2026",
        "courserun_title": "Intro",
        "total_enrolled_learners": 50,
        "engaged_learners": 1,
        "engagement_rate_pct": 2.0,
        "total_videos_watched": 9,
        "avg_videos_per_engaged_learner": 9.0,
        "total_problems_attempted": 4,
        "avg_problems_per_engaged_learner": 4.0,
        "total_chatbot_interactions": 3,
        "chatbot_users": 2,
        "chatbot_adoption_pct": 4.0,
        "certificates_earned": 1,
    }
    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_fake_fetch_all([row]),
        ),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/content-engagement",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    (data,) = response.json()["data"]
    # Primary cohort still visible.
    assert data["total_enrolled_learners"] == 50
    # Sub-floor secondary counts nulled.
    assert data["engaged_learners"] is None
    assert data["chatbot_users"] is None
    assert data["certificates_earned"] is None
    # Everything derived from engaged_learners.
    assert data["engagement_rate_pct"] is None
    assert data["total_videos_watched"] is None
    assert data["avg_videos_per_engaged_learner"] is None
    assert data["total_problems_attempted"] is None
    assert data["avg_problems_per_engaged_learner"] is None
    # Everything derived from chatbot_users.
    assert data["total_chatbot_interactions"] is None
    assert data["chatbot_adoption_pct"] is None


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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contract-utilization",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 403


async def test_org_endpoint_403_for_non_member(app):
    # org_slug isn't in the caller's organization claim at all — rejected
    # before ever reaching the MITx Online manager round-trip.
    async with _client(app) as client:
        response = await client.get(
            f"/api/v1/analytics/organizations/{ORG_A_ID}/contract-utilization",
            headers={"X-Userinfo": _manager_header(OTHER_ORG_ID)},
        )
    assert response.status_code == 403
    assert "Not a member" in response.json()["detail"]


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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/enrollment-funnel",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == ORG_A_ID
    assert body["data"] == []
    assert body["as_of"].startswith("2026-07-02T04:00:00")


async def test_admin_endpoint_envelope_has_no_organization_key(app):
    row = {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "contract_pk": "contract-pk-1",
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
        new=_fake_fetch_all([row], total_count=12),
    ):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/admin/contract-health",
                headers={"X-Userinfo": _admin_header()},
            )
    assert response.status_code == 200
    body = response.json()
    assert "organization_id" not in body
    assert body["as_of"].startswith("2026-07-02T04:00:00")
    assert len(body["data"]) == 1
    # Admin spans all orgs, but is paged the same way and needs the same signal.
    assert body["total_count"] == 12


async def test_admin_endpoint_403_without_realm_role(app):
    async with _client(app) as client:
        response = await client.get(
            "/api/v1/analytics/admin/contract-health",
            headers={"X-Userinfo": _manager_header(ORG_A_ID)},  # no admin realm role
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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/program-funnel",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    assert response.json()["as_of"] is None


async def test_org_endpoint_applies_default_pagination_to_query(app):
    # A caller passing no page params still gets a bounded query — the whole
    # point of the DoS fix: an unbounded grain can't be pulled whole.
    captured = {}

    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_capture_page_query(captured),
        ),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/content-engagement",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    assert "LIMIT %s OFFSET %s" in captured["query"]
    assert "ORDER BY" in captured["query"]
    # (organization_id, limit=default 100, offset=0)
    assert captured["params"] == (ORG_A_ID, 100, 0)


async def test_org_endpoint_honors_limit_and_offset(app):
    captured = {}

    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_capture_page_query(captured),
        ),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/enrollment-funnel?limit=25&offset=50",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    assert captured["params"] == (ORG_A_ID, 25, 50)


async def test_org_endpoint_rejects_out_of_range_limit(app):
    # limit above max_page_size (1000) is a 422 before any query runs.
    with patch(
        "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
        new=AsyncMock(return_value=True),
    ):
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/content-engagement?limit=5000",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 422


async def test_admin_endpoint_applies_pagination(app):
    captured = {}

    with patch(
        "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
        new=_capture_page_query(captured),
    ):
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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contract-utilization",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 503


def _row_template() -> dict:
    return {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "contract_pk": "contract-pk-1",
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
