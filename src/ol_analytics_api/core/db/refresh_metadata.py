"""Last-refresh timestamp for a StarRocks materialized view.

Every analytics response carries an ``as_of`` field so the dashboard can
tell a manager how fresh the numbers are (see the Analytics API Endpoints
and Verification & QA epics — ``as_of`` must be displayed). The MVs are
refreshed on a daily Dagster cadence, so the honest value to report for an
endpoint is *that endpoint's own MV's* ``LAST_REFRESH_FINISHED_TIME``, read
from StarRocks' own ``information_schema.materialized_views``.

This is deliberately per-MV, not a schema-wide ``MAX`` across every MV: if
one MV's daily refresh fails while the others succeed, a schema-wide MAX
would report the newest MV's time for the endpoint backed by the stale one —
confidently showing a fresh ``as_of`` over stale data, the exact opposite of
what the field is for. Keying freshness to the backing MV means a lagging MV
only ever understates its own endpoint's freshness, never another's.

The schema and MV names are passed as bound parameters, not spliced into the
query string, so they need no identifier validation here (unlike the data
queries, which qualify ``schema.table`` and go through build_select).

Because the value only moves once per daily refresh, it's cached per
(schema, MV) with a short TTL rather than re-queried on every analytics
request — the same bound-latency reasoning as the org-manager check cache.

A dashboard page load fires several org-scoped requests concurrently, so a
cold cache would otherwise let all of them race past the cache check and
each issue the same query. A per-(schema, MV) lock serializes the
cold-cache case onto a single query; double-checked locking (``if key in
_cache`` again after acquiring the lock) means a caller that had to wait
still gets a cache hit instead of querying again.

NOTE (StarRocks-version-specific): the column names ``LAST_REFRESH_FINISHED_TIME``,
``TABLE_SCHEMA`` and ``TABLE_NAME`` in ``information_schema.materialized_views``
vary across StarRocks builds (some expose ``MATERIALIZED_VIEW_DATABASE`` /
``MATERIALIZED_VIEW_NAME``). Verify against the live cluster during staging
smoke tests — a wrong column name fails this query at request time.
"""

from __future__ import annotations

import asyncio
import datetime
from collections import defaultdict

from cachetools import TTLCache

from ol_analytics_api.core.db.client import starrocks_pool

# The as_of value changes at most once per daily MV refresh, so a few
# minutes of staleness on the freshness timestamp is immaterial; caching it
# keeps the extra information_schema round-trip off the hot path.
_AS_OF_CACHE_TTL_SECONDS = 300
_cache: TTLCache[tuple[str, str], datetime.datetime | None] = TTLCache(
    maxsize=64, ttl=_AS_OF_CACHE_TTL_SECONDS
)
_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

_LATEST_REFRESH_QUERY = (
    "SELECT LAST_REFRESH_FINISHED_TIME AS as_of "
    "FROM information_schema.materialized_views "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)


async def latest_refresh_timestamp(schema: str, table: str) -> datetime.datetime | None:
    """Last-refresh time of ``schema.table`` (a materialized view), or ``None``
    if that MV has never finished a refresh (or is absent from
    information_schema)."""
    key = (schema, table)
    if key in _cache:
        return _cache[key]

    async with _locks[key]:
        if key in _cache:
            return _cache[key]
        rows = await starrocks_pool.fetch_all(_LATEST_REFRESH_QUERY, (schema, table))
        as_of = rows[0]["as_of"] if rows else None
        _cache[key] = as_of
        return as_of


def _clear_cache() -> None:
    """Test hook — the cache is process-global, so a test that stubs a
    particular as_of must start from an empty cache."""
    _cache.clear()
    _locks.clear()
