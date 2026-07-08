"""Shared query -> suppress -> construct helper.

Every read endpoint in this service follows the same three steps: run a
query against the shared StarRocks pool, suppress small-cohort rows, and
wrap the remainder in a response model. Collapsing that into one call means
a future change to the contract (error handling, suppressed-row logging,
etc.) is made in one place instead of once per endpoint.

This helper is also the anonymization chokepoint: it reads the row model's
declared ``cohort_policy`` and enforces the k-anonymity floor before any row
leaves the database layer. A model with no ``cohort_policy`` cannot be
returned through here, so shipping a new endpoint forces an explicit
governance decision about which columns are cohort counts rather than
letting rows out unsuppressed. Endpoints must go through this function
instead of calling ``starrocks_pool.fetch_all`` directly.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, cast

from sqlmodel import SQLModel

from ol_analytics_api.core.anonymization import CohortPolicy, suppress_small_cohorts
from ol_analytics_api.core.db.client import starrocks_pool


class SuppressibleModel(Protocol):
    """A row model that declares how the anonymization floor applies to it."""

    cohort_policy: ClassVar[CohortPolicy]


async def fetch_and_suppress[ModelT: SQLModel](
    query: str,
    params: tuple[Any, ...],
    model_cls: type[ModelT],
    floor: int,
) -> list[ModelT]:
    if not hasattr(model_cls, "cohort_policy"):
        msg = (
            f"{model_cls.__name__} must declare a `cohort_policy` to be returned "
            "through the anonymization chokepoint"
        )
        raise TypeError(msg)
    suppressible_cls = cast("type[SuppressibleModel]", model_cls)
    rows = await starrocks_pool.fetch_all(query, params)
    suppressed = suppress_small_cohorts(rows, suppressible_cls.cohort_policy, floor)
    return [model_cls(**row) for row in suppressed]
