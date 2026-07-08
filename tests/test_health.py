from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_dashboard.app import TENANT_NAME
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import mitxonline_client

TENANT_READINESS_PATH = f"/health/readiness/{TENANT_NAME}/"


# main.create_app() may be called more than once across the test session
# (this fixture is one of several call sites), but it mounts a process-wide
# singleton tenant app — module scope keeps this file's app object stable
# across its own tests rather than re-wiring the shared tenant app per test.
@pytest.fixture(scope="module")
def app():
    return create_app()


async def _fake_get_ok(*_args: object, **_kwargs: object) -> None:
    return None


def _healthy_dependencies(stack: ExitStack) -> None:
    """Patch every dependency the health checks touch so all succeed: the
    shared StarRocks pool, and the b2b_dashboard tenant's registered MITx
    Online reachability check.

    Patches mitxonline_client's private `_client` attribute rather than its
    `check_reachable` method — register_readiness_check() captured a bound
    method reference at tenant-app-creation time (once, at import), so
    patching the `check_reachable` attribute afterwards wouldn't affect the
    already-registered callable. `_client` is read fresh on every call,
    so patching it here reaches the real, already-registered check.
    """
    stack.enter_context(
        patch("ol_analytics_api.core.health.starrocks_pool.ping", new=AsyncMock(return_value=None))
    )
    stack.enter_context(
        patch.object(mitxonline_client, "_client", SimpleNamespace(get=_fake_get_ok))
    )


async def test_liveness_never_checks_dependencies(app):
    # No dependency mocking here on purpose — liveness must not touch them.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/liveness/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_ok_when_shared_infra_healthy(app):
    with ExitStack() as stack:
        _healthy_dependencies(stack)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/readiness/")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readiness_503_when_starrocks_pool_unhealthy(app):
    with patch(
        "ol_analytics_api.core.health.starrocks_pool.ping",
        new=AsyncMock(side_effect=RuntimeError("pool not started")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/readiness/")
    assert response.status_code == 503


async def test_shared_readiness_ignores_tenant_upstream_outage(app):
    # The core tenant-isolation guarantee: the shared /health/readiness/ that
    # K8s probes must stay ready when only a single tenant's private upstream
    # (MITx Online) is down — otherwise that tenant's outage would pull the
    # whole pod and take every other tenant down with it. StarRocks (shared)
    # is healthy; `_client` unset (None) makes check_reachable() raise, the
    # same as a real unreachable state.
    with (
        patch("ol_analytics_api.core.health.starrocks_pool.ping", new=AsyncMock(return_value=None)),
        patch.object(mitxonline_client, "_client", None),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/readiness/")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_startup_mirrors_shared_readiness(app):
    with ExitStack() as stack:
        _healthy_dependencies(stack)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/startup/")
    assert response.status_code == 200
    assert response.json() == {"status": "started"}


async def test_startup_ignores_tenant_upstream_outage(app):
    # Same isolation guarantee as readiness: a tenant upstream being down at
    # startup must not stop the pod from ever coming up.
    with (
        patch("ol_analytics_api.core.health.starrocks_pool.ping", new=AsyncMock(return_value=None)),
        patch.object(mitxonline_client, "_client", None),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/startup/")
    assert response.status_code == 200


async def test_tenant_readiness_ok_when_all_dependencies_healthy(app):
    with ExitStack() as stack:
        _healthy_dependencies(stack)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(TENANT_READINESS_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_tenant_readiness_503_when_tenant_upstream_unreachable(app):
    # The tenant's own sub-path is where its upstream outage surfaces — 503
    # here (but a healthy shared /health/readiness/) is exactly the decoupling
    # this design provides.
    with (
        patch("ol_analytics_api.core.health.starrocks_pool.ping", new=AsyncMock(return_value=None)),
        patch.object(mitxonline_client, "_client", None),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(TENANT_READINESS_PATH)
    assert response.status_code == 503


async def test_tenant_readiness_503_when_shared_infra_unhealthy(app):
    # Shared infra down means the tenant can't serve either — its sub-path is
    # a superset check, so it also reports 503.
    with (
        patch(
            "ol_analytics_api.core.health.starrocks_pool.ping",
            new=AsyncMock(side_effect=RuntimeError("pool not started")),
        ),
        patch.object(mitxonline_client, "_client", SimpleNamespace(get=_fake_get_ok)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(TENANT_READINESS_PATH)
    assert response.status_code == 503


async def test_tenant_readiness_404_for_unknown_tenant(app):
    with ExitStack() as stack:
        _healthy_dependencies(stack)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/readiness/does-not-exist/")
    assert response.status_code == 404
