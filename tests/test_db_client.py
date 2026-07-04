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
