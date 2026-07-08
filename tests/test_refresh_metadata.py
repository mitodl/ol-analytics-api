"""Unit tests for the per-schema as_of cache and its cold-cache dedup.

See core/db/refresh_metadata.py's module docstring for why concurrent
callers on a cold cache must serialize onto a single query instead of each
issuing their own.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, patch

from ol_analytics_api.core.db.refresh_metadata import _clear_cache, latest_refresh_timestamp

_AS_OF = datetime.datetime(2026, 7, 2, 4, 0, 0)  # noqa: DTZ001


def _patch_fetch_all(fetch_all):
    return patch(
        "ol_analytics_api.core.db.refresh_metadata.starrocks_pool.fetch_all", new=fetch_all
    )


async def test_latest_refresh_timestamp_caches_after_first_call():
    _clear_cache()
    fetch_all = AsyncMock(return_value=[{"as_of": _AS_OF}])
    with _patch_fetch_all(fetch_all):
        assert await latest_refresh_timestamp("schema_a") == _AS_OF
        assert await latest_refresh_timestamp("schema_a") == _AS_OF

    fetch_all.assert_awaited_once()


async def test_concurrent_cold_cache_calls_dedup_to_one_query():
    """Regression test: several concurrent callers hitting a cold cache for
    the same schema must serialize onto a single fetch_all call via the
    per-schema lock, rather than each racing past the cache check."""
    _clear_cache()
    release = asyncio.Event()

    async def slow_fetch_all(*_args):
        await release.wait()
        return [{"as_of": _AS_OF}]

    fetch_all = AsyncMock(side_effect=slow_fetch_all)
    with _patch_fetch_all(fetch_all):
        tasks = [asyncio.create_task(latest_refresh_timestamp("schema_b")) for _ in range(5)]
        # Let every task run up to its suspension point (waiting on the
        # lock, or waiting on `release`) before letting the query resolve.
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks)

    assert results == [_AS_OF] * 5
    fetch_all.assert_awaited_once()


async def test_different_schemas_query_independently():
    _clear_cache()
    fetch_all = AsyncMock(return_value=[{"as_of": _AS_OF}])
    with _patch_fetch_all(fetch_all):
        await latest_refresh_timestamp("schema_c")
        await latest_refresh_timestamp("schema_d")

    assert fetch_all.await_count == 2
