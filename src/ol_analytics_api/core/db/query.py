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
from ol_analytics_api.core.db.identifiers import validate_sql_identifier


def build_select(
    schema: str,
    table: str,
    model_cls: type[SQLModel],
    *,
    filter_column: str | None = None,
    order_by: tuple[str, ...],
) -> str:
    """Build a paginated ``SELECT`` projecting exactly ``model_cls``'s columns.

    This is the *single* place identifiers are spliced into SQL in this
    service. StarRocks can't parameterize identifiers, so schema, table,
    projected column, filter-column, and order-by names are interpolated — but
    every one is first run through ``validate_sql_identifier``, so the returned
    string provably contains nothing but validated identifiers and ``%s``
    placeholders. Row *values* (the filter value, LIMIT, OFFSET) are always
    bound params, never spliced. Collapsing the previous per-endpoint
    ``SELECT * ... # noqa: S608`` lines into this one construct means there is
    exactly one identifier-splicing site to review, not one per endpoint.

    Projecting the model's own fields instead of ``SELECT *`` also makes the
    query<->model column contract explicit (see the schema-drift task): a
    column dbt adds but the model doesn't mirror is simply not selected, and a
    column dbt drops surfaces as a query error naming that column, rather than
    ``SELECT *`` silently feeding an unexpected column set into a strict model.
    """
    if not order_by:
        msg = "order_by must contain at least one column for deterministic pagination"
        raise ValueError(msg)
    columns = ", ".join(validate_sql_identifier(name) for name in model_cls.model_fields)
    schema_table = f"{validate_sql_identifier(schema)}.{validate_sql_identifier(table)}"
    order = ", ".join(validate_sql_identifier(column) for column in order_by)
    where = f" WHERE {validate_sql_identifier(filter_column)} = %s" if filter_column else ""
    # The single S608 suppression in the service: justified because every
    # interpolated token above is a validate_sql_identifier'd identifier.
    return f"SELECT {columns} FROM {schema_table}{where} ORDER BY {order} LIMIT %s OFFSET %s"  # noqa: S608


def build_count(
    schema: str,
    table: str,
    model_cls: type[SQLModel],
    *,
    filter_column: str | None = None,
) -> str:
    """Build the ``COUNT(*)`` matching what ``build_select`` yields across all
    pages, so a client can tell a full page from a truncated result set.

    Deliberately not a plain ``COUNT(*)`` over the view. Rows whose primary
    cohort is below the floor are dropped by ``suppress_small_cohorts`` after
    the query returns, so a raw count would exceed anything paging can reach —
    and subtracting the rows the caller does receive would tell them exactly
    how many sub-floor cohorts their org has, which is the disclosure the floor
    exists to prevent. Applying the same primary-cohort gate here in SQL keeps
    the total consistent with the data and discloses nothing the rows don't.

    Identifiers are spliced under ``build_select``'s rules — every token is
    ``validate_sql_identifier``'d; the floor and the filter value are bound.
    """
    policy = _require_cohort_policy(model_cls).cohort_policy
    schema_table = f"{validate_sql_identifier(schema)}.{validate_sql_identifier(table)}"
    org_filter = f"{validate_sql_identifier(filter_column)} = %s AND " if filter_column else ""
    cohort_gate = f"{validate_sql_identifier(policy.primary)} >= %s"
    # Same justification as build_select: every interpolated token is a
    # validate_sql_identifier'd identifier, and values are bound params.
    return f"SELECT COUNT(*) AS total_count FROM {schema_table} WHERE {org_filter}{cohort_gate}"  # noqa: S608


class SuppressibleModel(Protocol):
    """A row model that declares how the anonymization floor applies to it."""

    cohort_policy: ClassVar[CohortPolicy]


def _require_cohort_policy(model_cls: type[SQLModel]) -> type[SuppressibleModel]:
    """The chokepoint's gate. A model with no declared policy cannot be read
    through this module at all — neither its rows nor a count of them, since
    counting without applying the floor would leak what suppression hides."""
    if not hasattr(model_cls, "cohort_policy"):
        msg = (
            f"{model_cls.__name__} must declare a `cohort_policy` to be returned "
            "through the anonymization chokepoint"
        )
        raise TypeError(msg)
    return cast("type[SuppressibleModel]", model_cls)


async def fetch_and_suppress[ModelT: SQLModel](
    query: str,
    params: tuple[Any, ...],
    model_cls: type[ModelT],
    floor: int,
) -> list[ModelT]:
    suppressible_cls = _require_cohort_policy(model_cls)
    rows = await starrocks_pool.fetch_all(query, params)
    suppressed = suppress_small_cohorts(rows, suppressible_cls.cohort_policy, floor)
    return [model_cls(**row) for row in suppressed]


async def fetch_visible_count(query: str, params: tuple[Any, ...]) -> int:
    """Run a ``build_count`` query and return its single number."""
    rows = await starrocks_pool.fetch_all(query, params)
    return int(rows[0]["total_count"]) if rows else 0
