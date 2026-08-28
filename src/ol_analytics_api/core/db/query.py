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

from collections.abc import Collection, Mapping
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


def build_grain_scan(
    schema: str,
    table: str,
    model_cls: type[SQLModel],
    *,
    filter_columns: tuple[str, ...],
) -> str:
    """Build the read of a finer-grained MV that a coarse endpoint's cross-grain
    guard reasons over.

    Not ``build_select``: this read never reaches a caller. Its rows are fed to
    ``hidden_additive_columns``, which suppresses them exactly as the finer
    grain's own endpoint would and reports which additive columns come back
    NULL. So it projects the finer model's full column set (the cohort policy
    needs every cohort it names) and takes no offset — the guard has to see all
    of them at once, because a coarse row can be reconstructed from finer rows
    the caller reads on any page of the finer endpoint.

    The bound ``LIMIT`` is a backstop, not paging. The caller must treat a full
    result as a truncated one and fail closed; see ``routers.organizations``.

    Identifiers are spliced under ``build_select``'s rules — every token is
    ``validate_sql_identifier``'d, the filter values and the limit are bound.
    """
    if not filter_columns:
        msg = "build_grain_scan needs at least one filter column"
        raise ValueError(msg)
    columns = ", ".join(validate_sql_identifier(name) for name in model_cls.model_fields)
    schema_table = f"{validate_sql_identifier(schema)}.{validate_sql_identifier(table)}"
    predicates = " AND ".join(f"{validate_sql_identifier(name)} = %s" for name in filter_columns)
    # Same justification as build_select: identifiers validated, values bound.
    return f"SELECT {columns} FROM {schema_table} WHERE {predicates} LIMIT %s"  # noqa: S608


class SuppressibleModel(Protocol):
    """A row model that declares how the anonymization floor applies to it."""

    cohort_policy: ClassVar[CohortPolicy]


def cohort_policy_of(model_cls: type[SQLModel]) -> CohortPolicy:
    """The policy a model declares, through the same gate the chokepoint uses.

    For callers that need to reason about a model's cohorts without reading its
    rows — the cross-grain guard suppresses a finer grain's rows to learn what
    it hides, and needs that grain's own policy to do it.
    """
    return _require_cohort_policy(model_cls).cohort_policy


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
    cross_grain: tuple[CrossGrainAdditives, Mapping[Any, Collection[str]]] | None = None,
) -> list[ModelT]:
    """Query, suppress, construct — the one path rows take out of the database.

    ``cross_grain`` is for a coarse-grained endpoint whose rows are sums over a
    finer grain that this service also publishes: pass the additive columns
    together with what the finer grain hides per key (see
    ``anonymization.hidden_additive_columns``) and those columns are blanked
    before any row becomes a model. Both suppression passes run here rather than
    in the router so that no endpoint can construct a response model from an
    unsuppressed row.
    """
    suppressible_cls = _require_cohort_policy(model_cls)
    rows = await starrocks_pool.fetch_all(query, params)
    suppressed = suppress_small_cohorts(rows, suppressible_cls.cohort_policy, floor)
    if cross_grain is not None:
        additives, hidden_by_key = cross_grain
        suppressed = suppress_cross_grain_additives(suppressed, additives, hidden_by_key)
    return [model_cls(**row) for row in suppressed]


async def fetch_grain_scan(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Run a ``build_grain_scan`` query, returning its raw unsuppressed rows.

    The only caller is a cross-grain guard, which suppresses them itself and
    keeps nothing but the set of columns that came back NULL. Nothing from here
    may be returned to a caller — that is what ``fetch_and_suppress`` is for.
    """
    return await starrocks_pool.fetch_all(query, params)


async def fetch_visible_count(query: str, params: tuple[Any, ...]) -> int:
    """Run a ``build_count`` query and return its single number."""
    rows = await starrocks_pool.fetch_all(query, params)
    return int(rows[0]["total_count"]) if rows else 0
