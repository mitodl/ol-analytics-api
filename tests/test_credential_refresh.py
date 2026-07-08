"""Regression tests for the Vault-credential refresh loop.

Vault-issued StarRocks credentials are a dynamic user with a lease; once it
expires, Vault revokes that user and the connection pool would start
failing authentication on every new connection. These tests exercise the
background refresh loop directly (patching asyncio.sleep so nothing here
waits on real wall-clock time) rather than through the full lifespan, so
the rotation and retry-on-failure behavior is covered without needing a
real Vault or StarRocks server.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ol_analytics_api.main import (
    _CREDENTIAL_REFRESH_MIN_INTERVAL_SECONDS,
    _CREDENTIAL_REFRESH_RETRY_SECONDS,
    _next_refresh_delay,
    _refresh_starrocks_credentials_forever,
)


def test_next_refresh_delay_uses_80_percent_safety_margin():
    assert _next_refresh_delay(100) == 80.0


def test_next_refresh_delay_has_a_floor_for_short_leases():
    assert _next_refresh_delay(10) == _CREDENTIAL_REFRESH_MIN_INTERVAL_SECONDS


async def test_refresh_loop_rotates_pool_then_reschedules_from_new_lease(monkeypatch):
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    with (
        patch(
            "ol_analytics_api.main._resolve_starrocks_credentials",
            new=AsyncMock(return_value=("new-user", "new-password", 200)),
        ),
        patch("ol_analytics_api.main.starrocks_pool.rotate", new=AsyncMock()) as rotate,
        pytest.raises(asyncio.CancelledError),
    ):
        await _refresh_starrocks_credentials_forever(initial_lease_duration=100)

    rotate.assert_called_once_with("new-user", "new-password")
    # First sleep uses the initial lease (100 * 0.8); after a successful
    # rotation, the next sleep uses the newly-fetched lease (200 * 0.8).
    assert sleep_calls == [80.0, 160.0]


async def test_refresh_loop_retries_after_a_failed_refresh(monkeypatch):
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    with (
        patch(
            "ol_analytics_api.main._resolve_starrocks_credentials",
            new=AsyncMock(side_effect=RuntimeError("Vault unreachable")),
        ),
        patch("ol_analytics_api.main.starrocks_pool.rotate", new=AsyncMock()) as rotate,
        pytest.raises(asyncio.CancelledError),
    ):
        await _refresh_starrocks_credentials_forever(initial_lease_duration=100)

    # A failed refresh must not raise out of the loop (it would silently
    # kill the whole background task) and must not touch the pool.
    rotate.assert_not_called()
    assert sleep_calls == [80.0, _CREDENTIAL_REFRESH_RETRY_SECONDS]
