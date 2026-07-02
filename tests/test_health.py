from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.main import create_app


@pytest.fixture
def app():
    return create_app()


async def test_health(app):
    with (
        patch("ol_analytics_api.db.client.starrocks_pool.start", new=AsyncMock()),
        patch("ol_analytics_api.db.client.starrocks_pool.stop", new=AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
