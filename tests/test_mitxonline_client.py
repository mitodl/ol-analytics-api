from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from ol_analytics_api.tenants.b2b_dashboard.auth import require_org_manager
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import MITxOnlineClient

_BASE = "https://mitxonline.example.test"
ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"


def _mgr_url(organization_id: str) -> str:
    return f"{_BASE}/api/v0/b2b/manager/organizations/?sso_organization_id={organization_id}"


@pytest.fixture
async def client():
    c = MITxOnlineClient(base_url=_BASE)
    c.start()
    yield c
    await c.aclose()


async def test_check_reachable_raises_when_not_started():
    c = MITxOnlineClient(base_url=_BASE)
    with pytest.raises(RuntimeError, match="start\\(\\) must be called"):
        await c.check_reachable()


async def test_is_org_manager_true_when_filtered_result_non_empty(client, httpx_mock):
    # MITx Online filters its managed-orgs queryset by sso_organization_id, so a
    # non-empty (paginated) result means the user manages this specific org.
    httpx_mock.add_response(url=_mgr_url(ORG_A), json={"count": 1, "results": [{"slug": "org-a"}]})
    assert await client.is_org_manager("user-true", ORG_A, "header-1") is True


async def test_is_org_manager_false_when_filtered_result_empty(client, httpx_mock):
    # Not a manager of this org -> the sso_organization_id filter yields no rows.
    httpx_mock.add_response(url=_mgr_url(ORG_B), json={"count": 0, "results": []})
    assert await client.is_org_manager("user-false", ORG_B, "header-1") is False


async def test_is_org_manager_tolerates_bare_list_response(client, httpx_mock):
    # Defensive: if the endpoint ever returns an unpaginated bare list.
    httpx_mock.add_response(url=_mgr_url(ORG_A), json=[{"slug": "org-a"}])
    assert await client.is_org_manager("user-bare", ORG_A, "header-1") is True


async def test_is_org_manager_reuses_one_httpx_client_across_calls(client, httpx_mock):
    # Two distinct (sub, org) pairs -> two real HTTP calls, no cache hit — the
    # assertion is on client *identity*, not just that requests succeed.
    httpx_mock.add_response(url=_mgr_url(ORG_A), json={"results": [{"slug": "org-a"}]})
    httpx_mock.add_response(url=_mgr_url(ORG_B), json={"results": [{"slug": "org-b"}]})

    client_before = client._client  # noqa: SLF001
    assert await client.is_org_manager("user-reuse-1", ORG_A, "header-1") is True
    assert client._client is client_before  # noqa: SLF001
    assert await client.is_org_manager("user-reuse-2", ORG_B, "header-2") is True
    assert client._client is client_before  # noqa: SLF001


async def test_is_org_manager_fails_closed_on_unexpected_shape(client, httpx_mock):
    # An error payload (a dict without `results`) -> results is None -> fail
    # closed instead of crashing. Not cached: an error, not a real answer.
    httpx_mock.add_response(url=_mgr_url("org-non-list"), json={"error": "internal server error"})
    assert await client.is_org_manager("user-non-list", "org-non-list", "header-1") is False


async def test_is_org_manager_caches_by_sub_and_org(client, httpx_mock):
    # Second call for the same (sub, organization_id) hits the cache, not a
    # second HTTP request.
    httpx_mock.add_response(url=_mgr_url("org-cache"), json={"results": [{"slug": "org-cache"}]})
    assert await client.is_org_manager("user-cache", "org-cache", "header-1") is True
    assert await client.is_org_manager("user-cache", "org-cache", "header-1") is True
    assert len(httpx_mock.get_requests()) == 1


async def test_require_org_manager_returns_503_not_raw_500_when_mitxonline_unreachable():
    userinfo = {"organization": {"an-alias": {"id": ORG_A}}, "sub": "user-x"}
    request = SimpleNamespace(headers={"X-Userinfo": "fake-header"})

    with (
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(side_effect=httpx.ConnectError("connection refused")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await require_org_manager(ORG_A, request, userinfo)

    assert exc_info.value.status_code == 503


async def test_require_org_manager_returns_502_when_mitxonline_returns_error_status():
    userinfo = {"organization": {"an-alias": {"id": ORG_A}}, "sub": "user-x"}
    request = SimpleNamespace(headers={"X-Userinfo": "fake-header"})
    upstream_response = httpx.Response(500, request=httpx.Request("GET", _BASE))

    with (
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "server error", request=upstream_response.request, response=upstream_response
                )
            ),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await require_org_manager(ORG_A, request, userinfo)

    assert exc_info.value.status_code == 502
