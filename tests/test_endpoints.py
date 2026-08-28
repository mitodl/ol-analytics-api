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
from ol_analytics_api.tenants.b2b_dashboard.routers import organizations

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


_CONTRACT_TREND_MV = "mv_b2b_contract_monthly_engagement_trend"


def _is_grain_scan(query):
    # The cross-grain guard's read of the contract-grained sibling. It is the
    # only query naming that MV on an org-scoped request.
    return _CONTRACT_TREND_MV in query


def _fake_fetch_all(data_rows, total_count=0, finer_rows=None):
    """A fetch_all stub that answers the information_schema as_of probe with
    _AS_OF, the envelope's total-count query with ``total_count``, the
    cross-grain scan of the contract-grained MV with ``finer_rows``, and every
    other query with the given MV rows."""

    async def fetch_all(query, *_args):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if _is_count_query(query):
            return [{"total_count": total_count}]
        if _is_grain_scan(query):
            return list(finer_rows or [])
        return list(data_rows)

    return fetch_all


def _capture_page_query(captured, rows=(), total_count=0):
    """A fetch_all stub that records the *paged* SELECT specifically. The as_of
    probe, the total-count query and the hidden-grain probe go through the same
    pool, so a stub that captured every query would report whichever happened
    to run last."""

    async def fetch_all(query, params):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if _is_count_query(query):
            return [{"total_count": total_count}]
        if _is_grain_scan(query):
            return []
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
            "contract_id": "101",
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
        "video_watchers": 1,
        "avg_videos_per_engaged_learner": 9.0,
        "total_problems_attempted": 4,
        "problem_attempters": 1,
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
    assert data["video_watchers"] is None
    assert data["problem_attempters"] is None
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


async def test_content_engagement_floors_activity_cohorts_under_a_large_engaged_cohort(app):
    # The gap PR #2520 closed: engaged_learners clears the floor, so nothing
    # keyed on it suppresses, but only ONE learner watched a video. Without
    # video_watchers in the row the totals and the average rode through on the
    # superset's floor and disclosed that learner's exact activity.
    row = {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "courserun_readable_id": "course-v1:MITx+6.00+2026",
        "courserun_title": "Intro",
        "total_enrolled_learners": 50,
        "engaged_learners": 30,
        "engagement_rate_pct": 60.0,
        "total_videos_watched": 9,
        "video_watchers": 1,
        "avg_videos_per_engaged_learner": 0.3,
        "total_problems_attempted": 400,
        "problem_attempters": 25,
        "avg_problems_per_engaged_learner": 13.3,
        "total_chatbot_interactions": 12,
        "chatbot_users": 8,
        "chatbot_adoption_pct": 16.0,
        "certificates_earned": 20,
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
    # The single video-watcher is suppressed, and so is everything computed
    # over that cohort -- including the average, whose numerator would
    # otherwise be recoverable as avg * engaged_learners.
    assert data["video_watchers"] is None
    assert data["total_videos_watched"] is None
    assert data["avg_videos_per_engaged_learner"] is None
    # Cohorts that cleared the floor are untouched: suppression is per-column,
    # so one sub-floor cohort must not blank the rest of the row.
    assert data["engaged_learners"] == 30
    assert data["engagement_rate_pct"] == 60.0
    assert data["problem_attempters"] == 25
    assert data["total_problems_attempted"] == 400
    assert data["avg_problems_per_engaged_learner"] == 13.3
    assert data["chatbot_users"] == 8
    assert data["total_chatbot_interactions"] == 12


async def test_monthly_trend_floors_event_counts_through_their_learner_cohorts(app):
    # An event count clears a learner floor on its own -- one learner
    # enrolling in twelve runs reads as new_enrollments == 12 -- so the floor
    # has to be applied to enrolling_learners and carried to the event count,
    # not applied to the event count directly.
    row = {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "activity_year_and_month": "2026-07",
        "monthly_active_learners": 40,
        "new_enrollments": 12,
        "enrolling_learners": 1,
        "certificates_earned": 30,
        "certified_learners": 22,
        "total_videos_watched": 500,
        "video_watchers": 18,
        "total_problems_attempted": 7,
        "problem_attempters": 2,
        "total_chatbot_interactions": 60,
        "chatbot_users": 15,
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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/engagement-trend",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    (data,) = response.json()["data"]
    # One enrolling learner: both the cohort and the 12 events it produced go.
    assert data["enrolling_learners"] is None
    assert data["new_enrollments"] is None
    # Two problem-attempters, likewise.
    assert data["problem_attempters"] is None
    assert data["total_problems_attempted"] is None
    # Cohorts above the floor keep their totals.
    assert data["monthly_active_learners"] == 40
    assert data["certified_learners"] == 22
    assert data["certificates_earned"] == 30
    assert data["video_watchers"] == 18
    assert data["total_videos_watched"] == 500
    assert data["chatbot_users"] == 15
    assert data["total_chatbot_interactions"] == 60


def _trend_row(month="2026-07"):
    """An org-grained engagement-trend row that clears every floor on its own,
    so anything suppressed in these tests came from the cross-grain guard."""
    return {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "activity_year_and_month": month,
        "monthly_active_learners": 40,
        "new_enrollments": 12,
        "enrolling_learners": 9,
        "certificates_earned": 30,
        "certified_learners": 22,
        "total_videos_watched": 500,
        "video_watchers": 18,
        "total_problems_attempted": 7,
        "problem_attempters": 6,
        "total_chatbot_interactions": 60,
        "chatbot_users": 15,
    }


def _contract_trend_row(contract, *, active, chatbot_users, chatbot_total, month="2026-07"):
    """A contract-grained trend row, the grain the org row sums over."""
    return _trend_row(month) | {
        "contract_pk": f"pk-{contract}",
        "contract_id": contract,
        "b2b_contract_name": contract,
        "monthly_active_learners": active,
        "chatbot_users": chatbot_users,
        "total_chatbot_interactions": chatbot_total,
    }


async def _get_trend(app, fetch_all):
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            return await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/engagement-trend",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )


async def test_org_trend_blanks_the_total_a_surviving_contract_row_withholds(app):
    # The contract row is NOT dropped: 30 active learners clears the row gate.
    # But only 2 of them used the chatbot, so the contract endpoint publishes
    # the row with its chatbot total withheld. Leave the org total alone and
    # `517 - 500` hands that withheld 17 back, attributable to those 2 learners.
    finer = [
        _contract_trend_row("C1", active=30, chatbot_users=2, chatbot_total=17),
        _contract_trend_row("C2", active=25, chatbot_users=20, chatbot_total=500),
    ]
    response = await _get_trend(app, _fake_fetch_all([_trend_row()], finer_rows=finer))

    assert response.status_code == 200
    (data,) = response.json()["data"]
    assert data["total_chatbot_interactions"] is None
    # Only the additive totals the contract grain actually withholds are
    # blanked per-column. The other totals are published in full downstream,
    # so nothing can be subtracted out of them and blanking them would cost
    # the dashboard data for no gain.
    assert data["total_videos_watched"] == 500
    assert data["total_problems_attempted"] == 7
    assert data["new_enrollments"] == 12
    # But every cohort column goes wholesale for this month, chatbot_users
    # included, even though that specific column isn't what triggered the
    # guard: two contracts sharing no learners would sum exactly, and nothing
    # here can distinguish that case from this one.
    assert data["monthly_active_learners"] is None
    assert data["chatbot_users"] is None
    assert data["certified_learners"] is None
    assert data["video_watchers"] is None
    assert data["problem_attempters"] is None
    assert data["enrolling_learners"] is None


async def test_org_trend_blanks_every_total_when_a_contract_row_is_dropped(app):
    # Below the row gate the contract contributes nothing visible at all, so
    # every column it fed into the org total is recoverable by subtraction.
    finer = [
        _contract_trend_row("C1", active=2, chatbot_users=2, chatbot_total=17),
        _contract_trend_row("C2", active=25, chatbot_users=20, chatbot_total=500),
    ]
    response = await _get_trend(app, _fake_fetch_all([_trend_row()], finer_rows=finer))

    (data,) = response.json()["data"]
    for column in (
        "new_enrollments",
        "certificates_earned",
        "total_videos_watched",
        "total_problems_attempted",
        "total_chatbot_interactions",
        "monthly_active_learners",
        "enrolling_learners",
        "certified_learners",
        "video_watchers",
        "problem_attempters",
        "chatbot_users",
    ):
        assert data[column] is None, column


async def test_org_trend_blanks_the_headline_count_disjoint_contracts_would_reveal(app):
    # The counterexample the guard closes: C1 (2 active, dropped) and C2 (25
    # active, published) share no learners, so the org total is their exact
    # sum. Left alone, `27 - 25` would hand back C1's suppressed headcount
    # exactly -- the failure mode `monthly_active_learners` staying published
    # was supposed to be safe from, on the assumption contracts overlap.
    org_row = _trend_row() | {"monthly_active_learners": 27}
    finer = [
        _contract_trend_row("C1", active=2, chatbot_users=1, chatbot_total=3),
        _contract_trend_row("C2", active=25, chatbot_users=20, chatbot_total=500),
    ]
    response = await _get_trend(app, _fake_fetch_all([org_row], finer_rows=finer))

    assert response.status_code == 200
    (data,) = response.json()["data"]
    assert data["monthly_active_learners"] is None


async def test_org_trend_untouched_when_the_contract_grain_publishes_in_full(app):
    finer = [_contract_trend_row("C1", active=40, chatbot_users=15, chatbot_total=60)]
    response = await _get_trend(app, _fake_fetch_all([_trend_row()], finer_rows=finer))

    assert response.status_code == 200
    (data,) = response.json()["data"]
    assert data["new_enrollments"] == 12
    assert data["total_videos_watched"] == 500
    assert data["certificates_earned"] == 30
    assert data["total_chatbot_interactions"] == 60


async def test_org_trend_blanks_only_the_months_the_contract_grain_withholds(app):
    rows = [_trend_row("2026-06"), _trend_row("2026-07")]
    finer = [
        _contract_trend_row("C1", active=40, chatbot_users=15, chatbot_total=60, month="2026-06"),
        _contract_trend_row("C1", active=30, chatbot_users=2, chatbot_total=17, month="2026-07"),
        _contract_trend_row("C2", active=25, chatbot_users=20, chatbot_total=500, month="2026-07"),
    ]
    response = await _get_trend(app, _fake_fetch_all(rows, finer_rows=finer))

    june, july = response.json()["data"]
    assert june["total_chatbot_interactions"] == 60
    assert july["total_chatbot_interactions"] is None


async def test_org_endpoints_without_a_finer_grain_issue_no_scan(app):
    # Only the trend endpoint aggregates across an org's contracts. Scanning on
    # the other four would be a round trip per request buying nothing.
    queries = []

    async def fetch_all(query, *_args):
        queries.append(query)
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if _is_count_query(query):
            return [{"total_count": 0}]
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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/content-engagement",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )

    assert response.status_code == 200
    assert not any(_is_grain_scan(query) for query in queries)


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
        "contract_id": "101",
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
        "contract_id": "101",
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


# ─── contract-scoped endpoints ────────────────────────────────────────────────


def _fake_fetch_all_with_contract(data_rows, *, contract_exists=True, total_count=0):
    """Like _fake_fetch_all, but also answers the contract gate's existence
    probe — `SELECT 1 ... LIMIT 1`, which returns a row when the contract
    belongs to the org and nothing when it does not."""

    async def fetch_all(query, *_args):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if query.startswith("SELECT 1 "):
            return [{"1": 1}] if contract_exists else []
        if _is_count_query(query):
            return [{"total_count": total_count}]
        return list(data_rows)

    return fetch_all


async def test_contract_endpoint_filters_on_both_org_and_contract(app):
    # The org predicate must survive alongside the contract one: without it a
    # manager of one org could read another org's contract by naming its id.
    captured = {}

    async def fetch_all(query, params):
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if query.startswith("SELECT 1 "):
            return [{"1": 1}]
        if _is_count_query(query):
            captured["count_query"] = query
            captured["count_params"] = params
            return [{"total_count": 0}]
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
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contracts/101/engagement-trend",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    assert "sso_organization_id = %s AND contract_id = %s" in captured["query"]
    # Both scope values bound, never spliced, and in the order the SQL names them.
    assert captured["params"][:2] == (ORG_A_ID, "101")
    # The count query carries the same two predicates plus the cohort gate.
    assert "sso_organization_id = %s AND contract_id = %s" in captured["count_query"]
    assert captured["count_params"][:2] == (ORG_A_ID, "101")


async def test_contract_endpoint_reads_the_contract_grained_mv(app):
    captured = {}

    async def fetch_all(query, params):  # noqa: ARG001
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if query.startswith("SELECT 1 "):
            return [{"1": 1}]
        if _is_count_query(query):
            return [{"total_count": 0}]
        captured["query"] = query
        return []

    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contracts/101/content-engagement",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    # Not the org-grained view: that one has no contract column to filter on.
    assert "mv_b2b_contract_content_engagement_depth" in captured["query"]


async def test_contract_not_in_org_is_403_not_an_empty_result(app):
    # An empty result would be indistinguishable from a contract of your own
    # that has no data yet. MITx Online refuses this request, so we do too.
    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_fake_fetch_all_with_contract([], contract_exists=False),
        ),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contracts/999/enrollment-funnel",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 403
    assert "999" in response.json()["detail"]


async def test_contract_route_still_requires_org_management(app):
    # The contract gate must not become a way around the org gate: a
    # non-manager is refused before the contract is ever looked up.
    probed = []

    async def fetch_all(query, *_args):
        probed.append(query)
        return []

    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=False),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contracts/101/contract-utilization",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 403
    assert probed == [], "the contract existence probe must not run for a non-manager"


async def test_contract_endpoint_suppresses_below_the_floor(app):
    # Contract grain does not weaken the floor: the same per-row gate applies.
    row = {
        "organization_key": "org-a",
        "organization_name": "Org A",
        "contract_pk": "contract-pk-1",
        "contract_id": "101",
        "b2b_contract_name": "C1",
        "activity_year_and_month": "2026-07",
        "monthly_active_learners": 40,
        "new_enrollments": 12,
        "enrolling_learners": 2,
        "certificates_earned": 30,
        "certified_learners": 22,
        "total_videos_watched": 500,
        "video_watchers": 18,
        "total_problems_attempted": 7,
        "problem_attempters": 1,
        "total_chatbot_interactions": 60,
        "chatbot_users": 15,
    }
    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=_fake_fetch_all_with_contract([row]),
        ),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/analytics/organizations/{ORG_A_ID}/contracts/101/engagement-trend",
                headers={"X-Userinfo": _manager_header(ORG_A_ID)},
            )
    assert response.status_code == 200
    (data,) = response.json()["data"]
    # Inherited cohort_policy, so the event counts still ride their cohorts.
    assert data["enrolling_learners"] is None
    assert data["new_enrollments"] is None
    assert data["problem_attempters"] is None
    assert data["total_problems_attempted"] is None
    # Contract identity is never suppressed — it is not a cohort.
    assert data["contract_id"] == "101"
    assert data["monthly_active_learners"] == 40


async def test_org_trend_fails_closed_when_the_finer_grain_scan_is_truncated(app):
    # A truncated scan cannot prove it saw every contributing contract row, so
    # the guard cannot say which totals are safe. Publishing an unchecked total
    # is the exact failure the guard exists to prevent, so the request fails.
    limit = organizations._GRAIN_SCAN_LIMIT  # noqa: SLF001
    finer = [
        _contract_trend_row(f"C{index}", active=30, chatbot_users=15, chatbot_total=60)
        for index in range(limit)
    ]
    with pytest.raises(RuntimeError, match="cross-grain guard cannot be applied"):
        await _get_trend(app, _fake_fetch_all([_trend_row()], finer_rows=finer))
