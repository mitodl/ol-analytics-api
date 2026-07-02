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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiomysql

from ol_analytics_api.core.config import settings


class StarRocksPool:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def start(self, user: str, password: str) -> None:
        self._pool = await aiomysql.create_pool(
            host=settings.starrocks_host,
            port=settings.starrocks_port,
            user=user,
            password=password,
            minsize=settings.starrocks_pool_min_size,
            maxsize=settings.starrocks_pool_max_size,
            autocommit=True,
            cursorclass=aiomysql.cursors.DictCursor,
        )

    async def stop(self) -> None:
        if self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[aiomysql.cursors.DictCursor]:
        if self._pool is None:
            msg = "StarRocksPool.start() must be called before use"
            raise RuntimeError(msg)
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            yield cur

    async def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.cursor() as cur:
            await cur.execute(query, params)
            return list(await cur.fetchall())

    async def ping(self) -> bool:
        if self._pool is None:
            return False
        async with self.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()
        return True


starrocks_pool = StarRocksPool()
