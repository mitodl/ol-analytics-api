import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ol_analytics_api.core.config import settings
from ol_analytics_api.core.db.client import PoolAcquireTimeoutError, StarRocksPool


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


async def test_create_pool_sets_dos_bounds():
    # The DoS-surface bounds must actually reach aiomysql: a per-statement
    # query_timeout (server-side, via init_command), a connect timeout, and
    # pool_recycle. Without these a hung/heavy query holds a pooled connection
    # indefinitely.
    pool = StarRocksPool()
    with patch(
        "ol_analytics_api.core.db.client.aiomysql.create_pool", new=AsyncMock()
    ) as create_pool:
        await pool._create_pool("u", "p")  # noqa: SLF001
    kwargs = create_pool.await_args.kwargs
    assert kwargs["connect_timeout"] == settings.starrocks_connect_timeout_seconds
    assert kwargs["pool_recycle"] == settings.starrocks_pool_recycle_seconds
    assert kwargs["init_command"] == (
        f"SET query_timeout = {settings.starrocks_query_timeout_seconds}"
    )


async def test_cursor_raises_pool_acquire_timeout_when_pool_saturated():
    # A saturated pool must fail fast rather than block forever: acquire() is
    # wrapped in a bounded wait_for, and a timeout surfaces as
    # PoolAcquireTimeoutError (mapped to 503 at the HTTP boundary) — this is
    # what keeps readiness ping() from hanging on an exhausted pool.
    async def never_returns():
        await asyncio.Event().wait()

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(side_effect=never_returns)

    pool = StarRocksPool()
    pool._pool = fake_pool  # noqa: SLF001
    with (
        patch.object(settings, "starrocks_pool_acquire_timeout_seconds", 0.01),
        pytest.raises(PoolAcquireTimeoutError, match="pool saturated"),
    ):
        async with pool.cursor():
            pass


async def test_stop_is_a_noop_when_never_started():
    pool = StarRocksPool()
    await pool.stop()  # must not raise
    assert pool.is_started is False


async def test_stop_closes_the_pool_when_started():
    fake_pool = AsyncMock()
    fake_pool.close = MagicMock()

    pool = StarRocksPool()
    with patch.object(pool, "_create_pool", new=AsyncMock(return_value=fake_pool)):
        await pool.start("user", "password")
    assert pool.is_started is True

    await pool.stop()

    fake_pool.close.assert_called_once()
    fake_pool.wait_closed.assert_awaited_once()
    assert pool.is_started is False


def _fake_cursor(rows):
    cur = MagicMock()
    cur.execute = AsyncMock()
    cur.fetchall = AsyncMock(return_value=rows)
    cur.fetchone = AsyncMock(return_value=rows[0] if rows else None)

    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=cur)
    cur_cm.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_cm)

    fake_pool = MagicMock()
    fake_pool.acquire = AsyncMock(return_value=conn)
    fake_pool.release = MagicMock()
    return fake_pool, cur


async def test_fetch_all_executes_query_and_returns_rows():
    fake_pool, cur = _fake_cursor([{"a": 1}, {"a": 2}])
    pool = StarRocksPool()
    pool._pool = fake_pool  # noqa: SLF001

    rows = await pool.fetch_all("SELECT a FROM t WHERE b = %s", ("x",))

    cur.execute.assert_awaited_once_with("SELECT a FROM t WHERE b = %s", ("x",))
    assert rows == [{"a": 1}, {"a": 2}]


async def test_ping_executes_select_1_when_pool_started():
    fake_pool, cur = _fake_cursor([{"1": 1}])
    pool = StarRocksPool()
    pool._pool = fake_pool  # noqa: SLF001

    await pool.ping()  # must not raise

    cur.execute.assert_awaited_once_with("SELECT 1")
    cur.fetchone.assert_awaited_once()


async def test_cursor_releases_connection_back_to_pool():
    # The connection must be returned to the pool even though acquire is now
    # done manually (not via aiomysql's own context manager).
    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cur_cm.__aexit__ = AsyncMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_cm)

    fake_pool = MagicMock()
    fake_pool.acquire = AsyncMock(return_value=conn)
    fake_pool.release = MagicMock()

    pool = StarRocksPool()
    pool._pool = fake_pool  # noqa: SLF001
    async with pool.cursor():
        pass
    fake_pool.release.assert_called_once_with(conn)
