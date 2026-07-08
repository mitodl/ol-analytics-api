"""Shared query -> suppress -> construct helper.

Every read endpoint in this service follows the same three steps: run a
query against the shared StarRocks pool, suppress small-cohort rows, and
wrap the remainder in a response model. Collapsing that into one call means
a future change to the contract (error handling, suppressed-row logging,
etc.) is made in one place instead of once per endpoint.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import SQLModel

from ol_analytics_api.core.anonymization import suppress_small_cohorts
from ol_analytics_api.core.db.client import starrocks_pool


async def fetch_and_suppress[ModelT: SQLModel](
    query: str,
    params: tuple[Any, ...],
    model_cls: type[ModelT],
    cohort_field: str,
    floor: int,
) -> list[ModelT]:
    rows = await starrocks_pool.fetch_all(query, params)
    return [model_cls(**row) for row in suppress_small_cohorts(rows, cohort_field, floor)]
