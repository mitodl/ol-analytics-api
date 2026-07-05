"""Regression tests for the root app's actual ASGI lifespan.

Every other test in this suite hits the app via httpx.AsyncClient +
ASGITransport, which never sends the lifespan.startup/shutdown ASGI
messages — so those tests can't catch a bug where a tenant's startup hook
silently never runs. This file uses asgi-lifespan's LifespanManager to
actually drive the real ASGI lifespan protocol end-to-end, the same way
uvicorn does. This is exactly the gap that let a real bug through: a
tenant's own `FastAPI(lifespan=...)` is never invoked when that tenant is
mounted via app.mount() (confirmed by reading Starlette's Router.lifespan(),
and by running the built Docker image, which is what actually caught it).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from asgi_lifespan import LifespanManager

from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import mitxonline_client


async def test_tenant_on_startup_and_on_shutdown_actually_run(monkeypatch):
    monkeypatch.setenv("STARROCKS_USER", "test")
    monkeypatch.setenv("STARROCKS_PASSWORD", "test")
    app = create_app()
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.start", new=AsyncMock()),
        patch("ol_analytics_api.core.db.client.starrocks_pool.stop", new=AsyncMock()),
    ):
        assert mitxonline_client._client is None  # noqa: SLF001
        async with LifespanManager(app):
            # b2b_dashboard.on_startup() calls mitxonline_client.start() —
            # if main.py's lifespan didn't call it (the bug this test
            # guards against), _client would still be None here.
            assert mitxonline_client._client is not None  # noqa: SLF001
        # on_shutdown() closes it back down.
        assert mitxonline_client._client is None  # noqa: SLF001
