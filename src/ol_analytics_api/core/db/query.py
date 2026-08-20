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

from collections.abc import Collection
from typing import Any, ClassVar, Protocol, cast

from sqlmodel import SQLModel

from ol_analytics_api.core.anonymization import (
    CohortPolicy,
    CrossGrainAdditives,
    suppress_cross_grain_additives,
    suppress_small_cohorts,
)
from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.db.identifiers import validate_sql_identifier


def build_select(
    schema: str,
    table: str,
    model_cls: type[SQLModel],
    *,
    filter_columns: tuple[str, ...] = (),
    order_by: tuple[str, ...],
) -> str:
    """Build a paginated ``SELECT`` projecting exactly ``model_cls``'s columns.

    This is the *single* place identifiers are spliced into SQL in this
    service. StarRocks can't parameterize identifiers, so schema, table,
    projected column, filter-column, and order-by names are interpolated — but
    every one is first run through ``validate_sql_identifier``, so the returned
    string provably contains nothing but validated identifiers and ``%s``
    placeholders. Row *values* (the filter values, LIMIT, OFFSET) are always
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
    # One bound placeholder per filter column, ANDed: org-scoped endpoints pass
    # just the org column, contract-scoped ones pass org AND contract. The org
    # predicate is never dropped when a contract is added -- a caller must not
    # be able to read another org's contract by naming it.
    predicates = " AND ".join(f"{validate_sql_identifier(name)} = %s" for name in filter_columns)
    where = f" WHERE {predicates}" if predicates else ""
    # The single S608 suppression in the service: justified because every
    # interpolated token above is a validate_sql_identifier'd identifier.
    return f"SELECT {columns} FROM {schema_table}{where} ORDER BY {order} LIMIT %s OFFSET %s"  # noqa: S608


def build_count(
    schema: str,
    table: str,
    model_cls: type[SQLModel],
    *,
    filter_columns: tuple[str, ...] = (),
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
    ``validate_sql_identifier``'d; the floor and the filter values are bound.
    """
    policy = _require_cohort_policy(model_cls).cohort_policy
    schema_table = f"{validate_sql_identifier(schema)}.{validate_sql_identifier(table)}"
    scope_filter = "".join(f"{validate_sql_identifier(name)} = %s AND " for name in filter_columns)
    cohort_gate = f"{validate_sql_identifier(policy.primary)} >= %s"
    # Same justification as build_select: every interpolated token is a
    # validate_sql_identifier'd identifier, and values are bound params.
    return f"SELECT COUNT(*) AS total_count FROM {schema_table} WHERE {scope_filter}{cohort_gate}"  # noqa: S608


def build_existence_check(schema: str, table: str, filter_columns: tuple[str, ...]) -> str:
    """A ``SELECT 1 ... LIMIT 1`` membership probe.

    Deliberately not routed through ``build_select``/``build_count``: it takes
    no model and reads no column, so the cohort-policy gate those enforce has
    nothing to enforce here. It exists for authorization checks that must
    distinguish "not yours" from "yours but empty" — see the b2b_dashboard
    tenant's contract gate — and it answers only with existence.

    It stays in this module so the identifier-splicing rule holds in one place:
    every token is ``validate_sql_identifier``'d, values are bound. Do not grow
    it into something that projects columns; that is what ``build_select`` is
    for, and the anonymization floor rides on that path.
    """
    if not filter_columns:
        msg = "build_existence_check needs at least one filter column"
        raise ValueError(msg)
    schema_table = f"{validate_sql_identifier(schema)}.{validate_sql_identifier(table)}"
    predicates = " AND ".join(f"{validate_sql_identifier(name)} = %s" for name in filter_columns)
    # Same justification as build_select: identifiers validated, values bound.
    return f"SELECT 1 FROM {schema_table} WHERE {predicates} LIMIT 1"  # noqa: S608


def build_hidden_grain_probe(
    schema: str,
    table: str,
    *,
    key_column: str,
    cohort_column: str,
    filter_columns: tuple[str, ...],
) -> str:
    """Build the probe that asks a finer-grained MV which keys it withholds.

    The service publishes the same learners at organization and contract grain,
    and a coarse row's exactly-additive columns are the sum of the contract rows
    beneath it. So a contract row dropped by the floor is recoverable as
    ``org_total - sum(the visible contract rows)``. Deciding whether to blank
    the coarse columns needs one bit per key: does the finer grain withhold a
    row here?

    That bit is all this returns. The comparison against the floor happens in
    SQL and the projection is the grouping key alone, so the sub-floor cohort
    count that motivates the whole exercise is never read into the process —
    it cannot be logged, serialized, or leaked by a later change to this file.

    NULL cohorts count as withheld: ``suppress_small_cohorts`` drops a row whose
    primary is NULL, and ``cohort < floor`` is NULL (not true) for those, so the
    predicate has to name them explicitly or the probe would miss exactly the
    rows the floor is most certain about.

    Identifiers are spliced under ``build_select``'s rules — every token is
    ``validate_sql_identifier``'d, the floor and filter values are bound.
    """
    if not filter_columns:
        msg = "build_hidden_grain_probe needs at least one filter column"
        raise ValueError(msg)
    schema_table = f"{validate_sql_identifier(schema)}.{validate_sql_identifier(table)}"
    key = validate_sql_identifier(key_column)
    cohort = validate_sql_identifier(cohort_column)
    predicates = " AND ".join(f"{validate_sql_identifier(name)} = %s" for name in filter_columns)
    # Same justification as build_select: identifiers validated, values bound.
    where = f"WHERE {predicates} AND ({cohort} < %s OR {cohort} IS NULL)"
    return f"SELECT DISTINCT {key} FROM {schema_table} {where}"  # noqa: S608


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
    *,
    cross_grain: tuple[CrossGrainAdditives, Collection[Any]] | None = None,
) -> list[ModelT]:
    """Query, suppress, construct — the one path rows take out of the database.

    ``cross_grain`` is for a coarse-grained endpoint whose rows are sums over a
    finer grain that this service also publishes: pass the additive columns
    together with the keys the finer grain withholds (see
    ``build_hidden_grain_probe``) and those columns are blanked before any row
    becomes a model. Both suppression passes run here rather than in the router
    so that no endpoint can construct a response model from an unsuppressed row.
    """
    suppressible_cls = _require_cohort_policy(model_cls)
    rows = await starrocks_pool.fetch_all(query, params)
    suppressed = suppress_small_cohorts(rows, suppressible_cls.cohort_policy, floor)
    if cross_grain is not None:
        additives, hidden_keys = cross_grain
        suppressed = suppress_cross_grain_additives(suppressed, additives, hidden_keys)
    return [model_cls(**row) for row in suppressed]


async def fetch_hidden_grain_keys(query: str, params: tuple[Any, ...]) -> frozenset[Any]:
    """Run a ``build_hidden_grain_probe`` query, returning the withheld keys."""
    rows = await starrocks_pool.fetch_all(query, params)
    return frozenset(next(iter(row.values())) for row in rows)


async def fetch_visible_count(query: str, params: tuple[Any, ...]) -> int:
    """Run a ``build_count`` query and return its single number."""
    rows = await starrocks_pool.fetch_all(query, params)
    return int(rows[0]["total_count"]) if rows else 0
