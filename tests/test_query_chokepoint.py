"""The fetch_and_suppress chokepoint must refuse to return rows for a model
that has not declared how the k-anonymity floor applies to it."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import SQLModel

from ol_analytics_api.core.db.query import fetch_and_suppress


class _PolicylessRow(SQLModel):
    value: int


async def test_fetch_and_suppress_rejects_model_without_cohort_policy():
    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=AsyncMock(return_value=[{"value": 1}]),
        ),
        pytest.raises(TypeError, match="cohort_policy"),
    ):
        await fetch_and_suppress("SELECT 1", (), _PolicylessRow, floor=5)
