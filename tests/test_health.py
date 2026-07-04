from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.main import create_app


@pytest.fixture
def app():
    return create_app()


async def test_liveness_never_checks_dependencies(app):
    # No StarRocks pool mocking here on purpose — liveness must not touch it.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/liveness/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_ok_when_starrocks_pool_healthy(app):
    with patch(
        "ol_analytics_api.core.health.starrocks_pool.ping", new=AsyncMock(return_value=True)
    ):
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


async def test_startup_mirrors_readiness(app):
    with patch(
        "ol_analytics_api.core.health.starrocks_pool.ping", new=AsyncMock(return_value=True)
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/startup/")
    assert response.status_code == 200
    assert response.json() == {"status": "started"}
