"""Last-refresh timestamp for a schema's StarRocks materialized views.

Every analytics response carries an ``as_of`` field so the dashboard can
tell a manager how fresh the numbers are (see the Analytics API Endpoints
and Verification & QA epics — ``as_of`` must be displayed). The MVs are
refreshed on a daily Dagster cadence, so the honest value to report is the
most recent ``LAST_REFRESH_FINISHED_TIME`` across the schema's MVs, read
from StarRocks' own ``information_schema.materialized_views``.

The schema name is passed as a bound parameter, not spliced into the query
string, so it needs no identifier validation here (unlike the data queries,
which qualify ``schema.table`` and must go through validate_sql_identifier).

Because the value only moves once per daily refresh, it's cached per schema
with a short TTL rather than re-queried on every analytics request — the
same bound-latency reasoning as the org-manager check cache.
"""

from __future__ import annotations

import datetime

from cachetools import TTLCache

from ol_analytics_api.core.db.client import starrocks_pool

# The as_of value changes at most once per daily MV refresh, so a few
# minutes of staleness on the freshness timestamp is immaterial; caching it
# keeps the extra information_schema round-trip off the hot path.
_AS_OF_CACHE_TTL_SECONDS = 300
_cache: TTLCache[str, datetime.datetime | None] = TTLCache(maxsize=64, ttl=_AS_OF_CACHE_TTL_SECONDS)

_LATEST_REFRESH_QUERY = (
    "SELECT MAX(LAST_REFRESH_FINISHED_TIME) AS as_of "
    "FROM information_schema.materialized_views "
    "WHERE TABLE_SCHEMA = %s"
)


async def latest_refresh_timestamp(schema: str) -> datetime.datetime | None:
    """Most recent MV refresh time in ``schema``, or ``None`` if no MV in
    that schema has ever finished a refresh."""
    if schema in _cache:
        return _cache[schema]

    rows = await starrocks_pool.fetch_all(_LATEST_REFRESH_QUERY, (schema,))
    as_of = rows[0]["as_of"] if rows else None
    _cache[schema] = as_of
    return as_of


def _clear_cache() -> None:
    """Test hook — the cache is process-global, so a test that stubs a
    particular as_of must start from an empty cache."""
    _cache.clear()
