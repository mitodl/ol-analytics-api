import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from ol_analytics_api.tenants.b2b_dashboard import mitxonline_client as client_module
from ol_analytics_api.tenants.b2b_dashboard.auth import require_org_manager
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import MITxOnlineClient

_BASE = "https://mitxonline.example.test"
_TOKEN_URL = f"{_BASE}/oauth2/token/"
ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"


def _check_url(organization_id: str, sub: str) -> str:
    return (
        f"{_BASE}/api/v0/b2b/service/organization-manager-check/"
        f"?sso_organization_id={organization_id}&user_global_id={sub}"
    )


def _token_response(httpx_mock, *, token: str = "tok-1", expires_in: int | None = 3600):
    body: dict[str, object] = {"access_token": token, "token_type": "Bearer"}
    if expires_in is not None:
        body["expires_in"] = expires_in
    httpx_mock.add_response(url=_TOKEN_URL, method="POST", json=body)


@pytest.fixture(autouse=True)
def _clear_manager_cache():
    # The org-manager cache is module-level, so it would otherwise leak
    # answers between tests.
    client_module._cache.clear()  # noqa: SLF001
    yield
    client_module._cache.clear()  # noqa: SLF001


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


async def test_is_org_manager_true(client, httpx_mock):
    _token_response(httpx_mock)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-true"), json={"is_manager": True})
    assert await client.is_org_manager("user-true", ORG_A) is True


async def test_is_org_manager_false(client, httpx_mock):
    _token_response(httpx_mock)
    httpx_mock.add_response(url=_check_url(ORG_B, "user-false"), json={"is_manager": False})
    assert await client.is_org_manager("user-false", ORG_B) is False


async def test_manager_check_is_sent_with_a_bearer_token(client, httpx_mock):
    # The whole point of the client-credentials swap: the call must carry a
    # service token, not a forwarded end-user identity.
    _token_response(httpx_mock, token="tok-bearer")
    httpx_mock.add_response(url=_check_url(ORG_A, "user-bearer"), json={"is_manager": True})

    await client.is_org_manager("user-bearer", ORG_A)

    check_request = httpx_mock.get_requests()[-1]
    assert check_request.headers["Authorization"] == "Bearer tok-bearer"
    assert "X-Userinfo" not in check_request.headers


async def test_token_request_uses_client_credentials_grant(client, httpx_mock):
    _token_response(httpx_mock)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-grant"), json={"is_manager": True})

    await client.is_org_manager("user-grant", ORG_A)

    token_request = httpx_mock.get_requests()[0]
    assert token_request.url == _TOKEN_URL
    assert b"grant_type=client_credentials" in token_request.read()
    # Client id/secret go in the Authorization header (HTTP Basic), not the body.
    assert token_request.headers["Authorization"].startswith("Basic ")


async def test_token_is_reused_across_calls(client, httpx_mock):
    # A cached, unexpired token must not be re-minted on every check.
    _token_response(httpx_mock, expires_in=3600)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-1"), json={"is_manager": True})
    httpx_mock.add_response(url=_check_url(ORG_B, "user-2"), json={"is_manager": True})

    assert await client.is_org_manager("user-1", ORG_A) is True
    assert await client.is_org_manager("user-2", ORG_B) is True

    token_requests = [r for r in httpx_mock.get_requests() if r.url == _TOKEN_URL]
    assert len(token_requests) == 1


async def test_concurrent_cold_misses_mint_one_token(client):
    # Without the lock, N simultaneous cache misses on a cold token would each
    # open their own token request. The second waiter must re-check inside the
    # lock and reuse what the first one minted.
    #
    # The mint is held open on an Event so both callers are genuinely in
    # flight at once; a mocked HTTP response returns without ever suspending,
    # which would let the first caller finish before the second starts and
    # quietly test nothing.
    release = asyncio.Event()
    calls = 0

    async def slow_fetch_token():
        nonlocal calls
        calls += 1
        await release.wait()
        client._token = "tok-concurrent"  # noqa: SLF001
        client._token_expires_at = time.monotonic() + 3600  # noqa: SLF001
        return "tok-concurrent"

    with patch.object(client, "_fetch_token", new=slow_fetch_token):
        first = asyncio.create_task(client._access_token())  # noqa: SLF001
        second = asyncio.create_task(client._access_token())  # noqa: SLF001
        # Let both reach the lock -- one holding it, one queued behind it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        tokens = await asyncio.gather(first, second)

    assert tokens == ["tok-concurrent", "tok-concurrent"]
    assert calls == 1


async def test_token_without_expires_in_is_not_cached(client, httpx_mock):
    # An absent expires_in means an unknown lifetime. Re-mint rather than
    # cache a token forever against a lifetime we don't know.
    _token_response(httpx_mock, expires_in=None)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-1"), json={"is_manager": True})
    _token_response(httpx_mock, expires_in=None)
    httpx_mock.add_response(url=_check_url(ORG_B, "user-2"), json={"is_manager": True})

    await client.is_org_manager("user-1", ORG_A)
    await client.is_org_manager("user-2", ORG_B)

    token_requests = [r for r in httpx_mock.get_requests() if r.url == _TOKEN_URL]
    assert len(token_requests) == 2


async def test_401_triggers_one_token_refresh_and_retry(client, httpx_mock):
    # A revoked or early-expired token should self-heal rather than fail the
    # user's request.
    _token_response(httpx_mock, token="stale")
    httpx_mock.add_response(url=_check_url(ORG_A, "user-401"), status_code=401)
    _token_response(httpx_mock, token="fresh")
    httpx_mock.add_response(url=_check_url(ORG_A, "user-401"), json={"is_manager": True})

    assert await client.is_org_manager("user-401", ORG_A) is True

    check_requests = [r for r in httpx_mock.get_requests() if "manager-check" in str(r.url)]
    assert len(check_requests) == 2
    assert check_requests[0].headers["Authorization"] == "Bearer stale"
    assert check_requests[1].headers["Authorization"] == "Bearer fresh"


async def test_persistent_401_propagates(client, httpx_mock):
    # A second 401 is a real misconfiguration, not a stale token -- surface it
    # rather than retrying forever.
    _token_response(httpx_mock, token="tok-a")
    httpx_mock.add_response(url=_check_url(ORG_A, "user-401x"), status_code=401)
    _token_response(httpx_mock, token="tok-b")
    httpx_mock.add_response(url=_check_url(ORG_A, "user-401x"), status_code=401)

    with pytest.raises(httpx.HTTPStatusError):
        await client.is_org_manager("user-401x", ORG_A)


async def test_missing_access_token_in_response_raises(client, httpx_mock):
    httpx_mock.add_response(url=_TOKEN_URL, method="POST", json={"token_type": "Bearer"})
    with pytest.raises(RuntimeError, match="no access_token"):
        await client.is_org_manager("user-no-token", ORG_A)


async def test_is_org_manager_reuses_one_httpx_client_across_calls(client, httpx_mock):
    _token_response(httpx_mock)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-reuse-1"), json={"is_manager": True})
    httpx_mock.add_response(url=_check_url(ORG_B, "user-reuse-2"), json={"is_manager": True})

    client_before = client._client  # noqa: SLF001
    assert await client.is_org_manager("user-reuse-1", ORG_A) is True
    assert client._client is client_before  # noqa: SLF001
    assert await client.is_org_manager("user-reuse-2", ORG_B) is True
    assert client._client is client_before  # noqa: SLF001


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "internal server error"},
        {"is_manager": "yes"},
        {},
        ["not", "a", "dict"],
    ],
    ids=["error-payload", "non-bool", "empty-dict", "bare-list"],
)
async def test_is_org_manager_fails_closed_on_unexpected_shape(client, httpx_mock, payload):
    # Anything that isn't a boolean `is_manager` is an error condition, not an
    # authorization answer -- fail closed rather than crash or guess.
    _token_response(httpx_mock)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-odd"), json=payload)
    assert await client.is_org_manager("user-odd", ORG_A) is False


async def test_unexpected_shape_is_not_cached(client, httpx_mock):
    # A transient glitch must not lock a legitimate manager out for the whole
    # cache TTL.
    _token_response(httpx_mock)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-glitch"), json={"error": "boom"})
    httpx_mock.add_response(url=_check_url(ORG_A, "user-glitch"), json={"is_manager": True})

    assert await client.is_org_manager("user-glitch", ORG_A) is False
    assert await client.is_org_manager("user-glitch", ORG_A) is True


async def test_is_org_manager_caches_by_sub_and_org(client, httpx_mock):
    _token_response(httpx_mock)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-cache"), json={"is_manager": True})

    assert await client.is_org_manager("user-cache", ORG_A) is True
    assert await client.is_org_manager("user-cache", ORG_A) is True

    check_requests = [r for r in httpx_mock.get_requests() if "manager-check" in str(r.url)]
    assert len(check_requests) == 1


async def test_aclose_discards_the_cached_token(client, httpx_mock):
    _token_response(httpx_mock)
    httpx_mock.add_response(url=_check_url(ORG_A, "user-close"), json={"is_manager": True})
    await client.is_org_manager("user-close", ORG_A)

    await client.aclose()

    assert client._token is None  # noqa: SLF001
    assert client._token_expires_at == 0.0  # noqa: SLF001


async def test_require_org_manager_returns_503_not_raw_500_when_mitxonline_unreachable():
    userinfo = {"organization": {"an-alias": {"id": ORG_A}}, "sub": "user-x"}

    with (
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(side_effect=httpx.ConnectError("connection refused")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await require_org_manager(ORG_A, userinfo)

    assert exc_info.value.status_code == 503


async def test_require_org_manager_returns_502_when_mitxonline_returns_error_status():
    userinfo = {"organization": {"an-alias": {"id": ORG_A}}, "sub": "user-x"}
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
        await require_org_manager(ORG_A, userinfo)

    assert exc_info.value.status_code == 502


async def test_require_org_manager_returns_403_when_not_a_manager():
    # The behaviour change that fixes the reported bug: a non-manager is now a
    # 403 from a 200 upstream response, not a 502 from an upstream 403.
    userinfo = {"organization": {"an-alias": {"id": ORG_A}}, "sub": "user-x"}

    with (
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=False),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await require_org_manager(ORG_A, userinfo)

    assert exc_info.value.status_code == 403
    assert "Not a manager" in exc_info.value.detail


async def test_require_org_manager_passes_for_a_manager():
    userinfo = {"organization": {"an-alias": {"id": ORG_A}}, "sub": "user-x"}

    with patch(
        "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
        new=AsyncMock(return_value=True),
    ):
        assert await require_org_manager(ORG_A, userinfo) is userinfo
