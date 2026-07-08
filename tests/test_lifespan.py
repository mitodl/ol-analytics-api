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

import pytest
from asgi_lifespan import LifespanManager

from ol_analytics_api.main import Tenant, create_app, lifespan
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


async def test_partial_tenant_startup_failure_still_stops_pool():
    """Regression test: if starrocks_pool.start() succeeds but a later
    tenant's on_startup() raises, the pool — and any tenant that did start
    — must still be torn down, not abandoned because the failure happened
    before the yield's try/finally."""
    started: list[str] = []
    shut_down: list[str] = []

    async def ok_startup() -> None:
        started.append("ok")

    async def ok_shutdown() -> None:
        shut_down.append("ok")

    async def failing_startup() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    fake_tenants = [
        Tenant("/ok", app=object(), on_startup=ok_startup, on_shutdown=ok_shutdown),
        Tenant("/broken", app=object(), on_startup=failing_startup),
    ]

    with (
        patch("ol_analytics_api.main.TENANTS", fake_tenants),
        patch(
            "ol_analytics_api.main._resolve_starrocks_credentials",
            new=AsyncMock(return_value=("user", "pass", None)),
        ),
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.start", new=AsyncMock()
        ) as pool_start,
        patch("ol_analytics_api.core.db.client.starrocks_pool.stop", new=AsyncMock()) as pool_stop,
        pytest.raises(RuntimeError, match="boom"),
    ):
        async with lifespan(None):
            pass

    pool_start.assert_awaited_once()
    pool_stop.assert_awaited_once()
    assert started == ["ok"]
    assert shut_down == ["ok"]
