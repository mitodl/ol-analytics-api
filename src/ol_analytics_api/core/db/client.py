"""Async connection pool for StarRocks over the MySQL wire protocol.

StarRocks exposes a MySQL-compatible protocol on port 9030. The official
`starrocks` Python connector wraps PyMySQL and is sync-only; `aiomysql`
speaks the same wire protocol and provides a native asyncio pool, so it's
used here to keep the service fully async.

One pool, shared by every mounted tenant — each tenant's queries qualify
their own schema (e.g. `b2b_analytics.mv_...`) rather than relying on a
connection-level default database, since different tenants may read from
different schemas over the same StarRocks cluster.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiomysql
import certifi

from ol_analytics_api.core.config import settings


class _FixedServerNameSSLContext(ssl.SSLContext):
    """Verifies the peer against ``server_name`` whatever host was dialed.

    aiomysql upgrades to TLS with ``server_hostname`` set to the host it
    connected to, and has no way to pass a different one. In-cluster that host
    is the FE Service's DNS name, while the FE presents the cert-manager
    certificate for the cluster's public domain."""

    server_name: str

    def wrap_bio(
        self,
        incoming: ssl.MemoryBIO,
        outgoing: ssl.MemoryBIO,
        server_side: bool = False,  # noqa: FBT001, FBT002
        server_hostname: str | bytes | None = None,  # noqa: ARG002
        session: ssl.SSLSession | None = None,
    ) -> ssl.SSLObject:
        return super().wrap_bio(
            incoming,
            outgoing,
            server_side=server_side,
            server_hostname=self.server_name,
            session=session,
        )


def starrocks_ssl_context(server_name: str) -> ssl.SSLContext | None:
    if not server_name:
        return None
    # PROTOCOL_TLS_CLIENT turns on CERT_REQUIRED and check_hostname.
    context = _FixedServerNameSSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    context.server_name = server_name
    return context


class PoolAcquireTimeoutError(Exception):
    """No StarRocks connection became free within
    ``starrocks_pool_acquire_timeout_seconds`` — the pool is saturated.

    Surfaced as HTTP 503 at the tenant-app boundary (see core/errors.py) so a
    caller fails fast rather than hanging on an exhausted pool. Because
    ping() acquires through the same path, this is also what keeps a
    saturated pool from blocking the readiness probe indefinitely (which would
    otherwise cascade the whole pod out of rotation)."""


class PlaintextConnectionError(Exception):
    """A pooled connection skipped TLS although
    ``starrocks_tls_server_name`` is set (see StarRocksPool.cursor)."""


class StarRocksPool:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    @property
    def is_started(self) -> bool:
        return self._pool is not None

    async def _create_pool(self, user: str, password: str) -> aiomysql.Pool:
        return await aiomysql.create_pool(
            host=settings.starrocks_host,
            port=settings.starrocks_port,
            user=user,
            password=password,
            minsize=settings.starrocks_pool_min_size,
            maxsize=settings.starrocks_pool_max_size,
            autocommit=True,
            cursorclass=aiomysql.cursors.DictCursor,
            ssl=starrocks_ssl_context(settings.starrocks_tls_server_name),
            # Bound new-connection establishment so a slow/unreachable
            # StarRocks can't wedge a pool fill.
            connect_timeout=settings.starrocks_connect_timeout_seconds,
            # Recycle connections older than this to shed half-dead sockets.
            pool_recycle=settings.starrocks_pool_recycle_seconds,
            # Server-side per-statement timeout, set once per connection:
            # StarRocks aborts any query running past `query_timeout` (seconds)
            # and frees the connection, so a heavy or runaway query can't hold
            # a pooled connection open indefinitely. The value is an int from
            # validated settings — safe to splice.
            init_command=f"SET query_timeout = {settings.starrocks_query_timeout_seconds}",
        )

    async def start(self, user: str, password: str) -> None:
        self._pool = await self._create_pool(user, password)

    async def stop(self) -> None:
        if self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None

    async def rotate(self, user: str, password: str) -> None:
        """Swap in a new pool built from fresh credentials, then close the
        old one. Used to move off Vault-issued credentials before their
        lease expires — see main.py's background refresh loop. New queries
        start using the new pool immediately; connections already checked
        out of the old pool finish naturally before it closes."""
        new_pool = await self._create_pool(user, password)
        old_pool, self._pool = self._pool, new_pool
        if old_pool is not None:
            old_pool.close()
            await old_pool.wait_closed()

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[aiomysql.cursors.DictCursor]:
        pool = self._pool
        if pool is None:
            msg = "StarRocksPool.start() must be called before use"
            raise RuntimeError(msg)
        # Bound the checkout: aiomysql's pool.acquire() waits forever on a
        # saturated pool, so wrap it in a timeout that fails fast instead.
        try:
            conn = await asyncio.wait_for(
                pool.acquire(), timeout=settings.starrocks_pool_acquire_timeout_seconds
            )
        except TimeoutError as exc:
            msg = (
                "Timed out acquiring a StarRocks connection after "
                f"{settings.starrocks_pool_acquire_timeout_seconds}s (pool saturated)"
            )
            raise PoolAcquireTimeoutError(msg) from exc
        try:
            # aiomysql upgrades to TLS only when the FE greeting offers it, and
            # otherwise carries on in plaintext, so an on-path peer can strip
            # TLS by clearing that flag. Refuse such a connection before any
            # query goes over it. aiomysql exposes no public flag for this.
            if settings.starrocks_tls_server_name and not conn._secure:  # noqa: SLF001
                conn.close()
                msg = "StarRocks FE did not negotiate TLS; refusing a plaintext connection"
                raise PlaintextConnectionError(msg)
            async with conn.cursor() as cur:
                yield cur
        finally:
            # aiomysql's Pool.release is synchronous, not a coroutine —
            # awaiting it raises TypeError and every checkout would fail.
            pool.release(conn)

    async def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.cursor() as cur:
            await cur.execute(query, params)
            return list(await cur.fetchall())

    async def ping(self) -> None:
        # No self._pool-is-None special case here — cursor() already raises
        # a clear RuntimeError in that case, which is exactly the "not
        # ready" signal a health check should propagate as a failure, not
        # silently swallow into a falsy-but-still-200 response.
        async with self.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()


starrocks_pool = StarRocksPool()
