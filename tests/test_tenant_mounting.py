from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.main import create_app


@pytest.fixture
def app():
    return create_app()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_b2b_dashboard_tenant_mounted_at_its_prefix(app):
    """The b2b_dashboard tenant's own route ("/organizations/...") resolves
    at the root app's mount path ("/api/v1/analytics"), and is gated by that
    tenant's own auth — proving root main.py and the tenant sub-app compose
    correctly, not just that each imports cleanly."""
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.start", new=AsyncMock()),
        patch("ol_analytics_api.core.db.client.starrocks_pool.stop", new=AsyncMock()),
    ):
        async with await _client(app) as client:
            response = await client.get(
                "/api/v1/analytics/organizations/some-org/contract-utilization"
            )
    # No X-Userinfo header -> this tenant's require_org_manager dependency
    # rejects before any StarRocks query runs.
    assert response.status_code == 401


async def test_unmounted_path_is_404(app):
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.start", new=AsyncMock()),
        patch("ol_analytics_api.core.db.client.starrocks_pool.stop", new=AsyncMock()),
    ):
        async with await _client(app) as client:
            response = await client.get("/api/v1/some-other-tenant/whatever")
    assert response.status_code == 404


async def test_tenant_request_logged_exactly_once(app):
    """Regression test: add_request_logging() used to be called both on the
    root app and inside the tenant's own create_app(), so Starlette ran both
    middleware stacks and every tenant request produced two access-log
    lines. Now it's applied once, centrally, in main.py's mount loop."""
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.start", new=AsyncMock()),
        patch("ol_analytics_api.core.db.client.starrocks_pool.stop", new=AsyncMock()),
        patch("ol_analytics_api.core.observability.middleware.log.info") as log_info,
    ):
        async with await _client(app) as client:
            await client.get("/api/v1/analytics/organizations/some-org/contract-utilization")
    assert log_info.call_count == 1
