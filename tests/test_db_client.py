import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ol_analytics_api.core.config import settings
from ol_analytics_api.core.db.client import (
    PlaintextConnectionError,
    PoolAcquireTimeoutError,
    StarRocksPool,
    starrocks_ssl_context,
)


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


async def test_create_pool_connects_in_plaintext_without_a_tls_server_name():
    pool = StarRocksPool()
    with (
        patch.object(settings, "starrocks_tls_server_name", ""),
        patch(
            "ol_analytics_api.core.db.client.aiomysql.create_pool", new=AsyncMock()
        ) as create_pool,
    ):
        await pool._create_pool("u", "p")  # noqa: SLF001
    assert create_pool.await_args.kwargs["ssl"] is None


async def test_create_pool_verifies_the_fe_certificate_when_a_tls_server_name_is_set():
    pool = StarRocksPool()
    with (
        patch.object(settings, "starrocks_tls_server_name", "lakehouse.starrocks.ol.mit.edu"),
        patch(
            "ol_analytics_api.core.db.client.aiomysql.create_pool", new=AsyncMock()
        ) as create_pool,
    ):
        await pool._create_pool("u", "p")  # noqa: SLF001
    context = create_pool.await_args.kwargs["ssl"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ssl_context_checks_the_configured_name_not_the_dialed_host():
    # aiomysql hands asyncio the in-cluster Service name as server_hostname;
    # the handshake must check the certificate's public name instead.
    context = starrocks_ssl_context("lakehouse.starrocks.ol.mit.edu")
    assert context is not None
    ssl_object = context.wrap_bio(
        ssl.MemoryBIO(),
        ssl.MemoryBIO(),
        server_hostname="lakehouse-starrocks-fe-service.starrocks.svc.cluster.local",
    )
    assert ssl_object.server_hostname == "lakehouse.starrocks.ol.mit.edu"


def _pool_handing_out(conn):
    fake_pool = MagicMock()
    fake_pool.acquire = AsyncMock(return_value=conn)
    fake_pool.release = MagicMock()
    pool = StarRocksPool()
    pool._pool = fake_pool  # noqa: SLF001
    return pool, fake_pool


async def test_cursor_refuses_a_plaintext_connection_when_tls_is_configured():
    # aiomysql silently stays in plaintext when the FE greeting doesn't offer
    # TLS (or something on the path strips the flag); no query may run on it.
    conn = MagicMock(_secure=False)
    pool, fake_pool = _pool_handing_out(conn)
    with (
        patch.object(settings, "starrocks_tls_server_name", "lakehouse.starrocks.ol.mit.edu"),
        pytest.raises(PlaintextConnectionError),
    ):
        async with pool.cursor():
            pass
    conn.cursor.assert_not_called()
    conn.close.assert_called_once()
    fake_pool.release.assert_called_once_with(conn)


async def test_cursor_accepts_a_tls_connection_when_tls_is_configured():
    conn = MagicMock(_secure=True)
    conn.cursor.return_value.__aenter__ = AsyncMock()
    conn.cursor.return_value.__aexit__ = AsyncMock(return_value=False)
    pool, _ = _pool_handing_out(conn)
    with patch.object(settings, "starrocks_tls_server_name", "lakehouse.starrocks.ol.mit.edu"):
        async with pool.cursor():
            pass
    conn.close.assert_not_called()


async def test_aiomysql_still_tracks_tls_on_the_connection():
    # cursor() reads aiomysql's private _secure flag; fail here, not in QA,
    # if an aiomysql upgrade drops it.
    import aiomysql  # noqa: PLC0415

    assert aiomysql.Connection()._secure is False  # noqa: SLF001


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
