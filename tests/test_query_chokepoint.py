"""The query chokepoint must refuse to read — as rows or as a count — for a
model that has not declared how the k-anonymity floor applies to it."""

from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import SQLModel

from ol_analytics_api.core.anonymization import CohortPolicy
from ol_analytics_api.core.db.query import build_count, fetch_and_suppress, fetch_visible_count


class _PolicylessRow(SQLModel):
    value: int


class _PolicyRow(SQLModel):
    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(primary="enrolled_learners")

    enrolled_learners: int


async def test_fetch_and_suppress_rejects_model_without_cohort_policy():
    with (
        patch(
            "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
            new=AsyncMock(return_value=[{"value": 1}]),
        ),
        pytest.raises(TypeError, match="cohort_policy"),
    ):
        await fetch_and_suppress("SELECT 1", (), _PolicylessRow, floor=5)


def test_build_count_rejects_model_without_cohort_policy():
    # Counting without the floor applied would disclose exactly what
    # suppression hides, so the same gate guards the count query.
    with pytest.raises(TypeError, match="cohort_policy"):
        build_count("schema", "table", _PolicylessRow)


def test_build_count_gates_on_the_primary_cohort():
    query = build_count("b2b", "mv_thing", _PolicyRow, filter_column="organization_key")

    # The floor is a bound param, never spliced; the gate makes the total match
    # what paging yields rather than counting rows suppression will drop.
    assert query == (
        "SELECT COUNT(*) AS total_count FROM b2b.mv_thing "
        "WHERE organization_key = %s AND enrolled_learners >= %s"
    )


def test_build_count_without_a_filter_column_still_gates_on_the_cohort():
    # The admin endpoint spans all orgs, so it has no org filter — but the
    # anonymization floor still applies.
    assert build_count("b2b", "mv_thing", _PolicyRow) == (
        "SELECT COUNT(*) AS total_count FROM b2b.mv_thing WHERE enrolled_learners >= %s"
    )


async def test_fetch_visible_count_returns_the_count():
    with patch(
        "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
        new=AsyncMock(return_value=[{"total_count": 42}]),
    ):
        assert await fetch_visible_count("SELECT COUNT(*)", ()) == 42


async def test_fetch_visible_count_of_an_empty_result_is_zero():
    with patch(
        "ol_analytics_api.core.db.client.starrocks_pool.fetch_all",
        new=AsyncMock(return_value=[]),
    ):
        assert await fetch_visible_count("SELECT COUNT(*)", ()) == 0
