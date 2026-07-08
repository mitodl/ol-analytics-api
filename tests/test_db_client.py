from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ol_analytics_api.core.db.client import StarRocksPool


async def test_ping_raises_when_pool_never_started():
    # Regression test: ping() used to special-case `self._pool is None` and
    # return False instead of raising, which health.py's readiness/startup
    # checks (only converting exceptions into a 503) silently treated as
    # healthy. ping() now has no special case — cursor()'s own RuntimeError
    # propagates, exactly like every other query would fail the same way.
    pool = StarRocksPool()
    assert pool.is_started is False
    with pytest.raises(RuntimeError, match="start\\(\\) must be called"):
        await pool.ping()


async def test_rotate_swaps_in_a_new_pool_and_closes_the_old_one():
    # Regression test: Vault-issued credentials expire on a lease; rotate()
    # is how the pool moves onto freshly-fetched credentials without a
    # restart (see main.py's background refresh loop).
    # aiomysql.Pool.close() is a plain synchronous method (only
    # wait_closed() is async) — override the auto-async default so the
    # mock matches the real API and assert_called_once() means what it says.
    old_pool = AsyncMock()
    old_pool.close = MagicMock()
    new_pool = AsyncMock()

    pool = StarRocksPool()
    with patch.object(pool, "_create_pool", new=AsyncMock(return_value=old_pool)):
        await pool.start("old-user", "old-password")
    assert pool._pool is old_pool  # noqa: SLF001

    with patch.object(pool, "_create_pool", new=AsyncMock(return_value=new_pool)) as create:
        await pool.rotate("new-user", "new-password")

    create.assert_called_once_with("new-user", "new-password")
    assert pool._pool is new_pool  # noqa: SLF001
    old_pool.close.assert_called_once()
    old_pool.wait_closed.assert_awaited_once()


async def test_rotate_before_start_just_sets_the_pool():
    new_pool = AsyncMock()
    pool = StarRocksPool()
    with patch.object(pool, "_create_pool", new=AsyncMock(return_value=new_pool)):
        await pool.rotate("new-user", "new-password")
    assert pool._pool is new_pool  # noqa: SLF001
