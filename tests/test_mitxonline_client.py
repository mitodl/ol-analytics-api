from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from ol_analytics_api.tenants.b2b_dashboard.auth import require_org_manager
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import MITxOnlineClient


@pytest.fixture
async def client():
    c = MITxOnlineClient(base_url="https://mitxonline.example.test")
    c.start()
    yield c
    await c.aclose()


async def test_check_reachable_raises_when_not_started():
    c = MITxOnlineClient(base_url="https://mitxonline.example.test")
    with pytest.raises(RuntimeError, match="start\\(\\) must be called"):
        await c.check_reachable()


async def test_is_org_manager_reuses_one_httpx_client_across_calls(client, httpx_mock):
    # Two distinct (sub, org) pairs -> two real HTTP calls, no cache hit —
    # if a new httpx.AsyncClient were constructed per call (the pre-fix
    # behavior), each would still "work" against the mock transport, so the
    # real assertion is on the *client identity*, not just that requests
    # succeed.
    httpx_mock.add_response(
        url="https://mitxonline.example.test/api/v0/b2b/manager/organizations/",
        json=[{"slug": "org-a"}],
    )
    httpx_mock.add_response(
        url="https://mitxonline.example.test/api/v0/b2b/manager/organizations/",
        json=[{"slug": "org-b"}],
    )

    client_before = client._client  # noqa: SLF001
    assert await client.is_org_manager("user-1", "org-a", "header-1") is True
    assert client._client is client_before  # noqa: SLF001
    assert await client.is_org_manager("user-2", "org-b", "header-2") is True
    assert client._client is client_before  # noqa: SLF001


async def test_is_org_manager_fails_closed_on_non_list_response(client, httpx_mock):
    # Regression test: an unexpected upstream shape (e.g. an error payload
    # that's a dict, not a list) used to crash with AttributeError inside
    # `.get()` instead of failing closed. Distinct (sub, org) pair from
    # other tests in this file — the TTLCache is module-level shared state,
    # so reusing a key another test already cached would mask this path.
    httpx_mock.add_response(
        url="https://mitxonline.example.test/api/v0/b2b/manager/organizations/",
        json={"error": "internal server error"},
    )
    assert await client.is_org_manager("user-non-list", "org-non-list", "header-1") is False


async def test_is_org_manager_ignores_non_dict_list_items(client, httpx_mock):
    httpx_mock.add_response(
        url="https://mitxonline.example.test/api/v0/b2b/manager/organizations/",
        json=["not-a-dict", {"slug": "org-mixed-list"}],
    )
    assert await client.is_org_manager("user-mixed-list", "org-mixed-list", "header-1") is True


async def test_require_org_manager_returns_503_not_raw_500_when_mitxonline_unreachable():
    userinfo = {"organization": {"my-org": {"id": "some-id"}}, "sub": "user-x"}
    request = SimpleNamespace(headers={"X-Userinfo": "fake-header"})

    with (
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(side_effect=httpx.ConnectError("connection refused")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await require_org_manager("my-org", request, userinfo)

    assert exc_info.value.status_code == 503


async def test_require_org_manager_returns_502_when_mitxonline_returns_error_status():
    userinfo = {"organization": {"my-org": {"id": "some-id"}}, "sub": "user-x"}
    request = SimpleNamespace(headers={"X-Userinfo": "fake-header"})
    upstream_response = httpx.Response(
        500, request=httpx.Request("GET", "https://mitxonline.example.test")
    )

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
        await require_org_manager("my-org", request, userinfo)

    assert exc_info.value.status_code == 502
