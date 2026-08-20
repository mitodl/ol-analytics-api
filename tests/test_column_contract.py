"""In-repo half of the SELECT<->model column contract (schema-drift task).

``build_select`` projects exactly each model's declared fields, so the query
and the strict SQLModel can't silently drift *within this repo*: an order-by or
cohort-policy column that isn't a real model field, or a projection that stops
matching the model, fails here instead of at request time. Every projected
value is also asserted to be a validate_sql_identifier-safe name — proving the
one identifier-splicing construct only ever emits validated identifiers.

The *other* half of the contract — dbt/StarRocks adding or dropping a column
the model doesn't mirror — needs a live StarRocks and is tracked as a separate
CI contract-test task (see the schema-drift follow-up).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlmodel import SQLModel

from ol_analytics_api.core.db.identifiers import validate_sql_identifier
from ol_analytics_api.core.db.query import build_select
from ol_analytics_api.tenants.b2b_dashboard.models import MitAdminContractHealth
from ol_analytics_api.tenants.b2b_dashboard.routers import admin, contracts, organizations


@dataclass(frozen=True)
class _Case:
    label: str
    query: str
    model: type[SQLModel]
    order_by: tuple[str, ...]


def _projected_columns(query: str) -> list[str]:
    select_clause = query.split(" FROM ", 1)[0].removeprefix("SELECT ")
    return [column.strip() for column in select_clause.split(",")]


# Every real endpoint: both declarative tables plus the admin endpoint's
# constants, each with the query build_select actually emits. The contract
# endpoints are built from contracts.ENDPOINTS rather than restated, so a row
# added there is covered here without touching this file.
_CASES = (
    [
        _Case(
            spec.mv,
            build_select(
                organizations._SCHEMA,  # noqa: SLF001
                spec.mv,
                spec.model,
                filter_columns=(organizations._ORG_FILTER_COLUMN,),  # noqa: SLF001
                order_by=spec.order_by,
            ),
            spec.model,
            spec.order_by,
        )
        for spec in organizations.ENDPOINTS
    ]
    + [
        _Case(
            f"{spec.mv} (contract-scoped)",
            build_select(
                contracts._SCHEMA,  # noqa: SLF001
                spec.mv,
                spec.model,
                filter_columns=contracts._FILTER_COLUMNS,  # noqa: SLF001
                order_by=spec.order_by,
            ),
            spec.model,
            spec.order_by,
        )
        for spec in contracts.ENDPOINTS
    ]
    + [
        _Case(
            admin._MV,  # noqa: SLF001
            build_select(
                "b2b_analytics",
                admin._MV,  # noqa: SLF001
                MitAdminContractHealth,
                order_by=admin._ORDER_BY,  # noqa: SLF001
            ),
            MitAdminContractHealth,
            admin._ORDER_BY,  # noqa: SLF001
        ),
    ]
)

_cases = pytest.mark.parametrize("case", _CASES, ids=[c.label for c in _CASES])


@_cases
def test_query_projects_exactly_model_fields(case):
    # No SELECT *, and the projection is exactly the model's fields in order.
    assert "SELECT *" not in case.query
    assert _projected_columns(case.query) == list(case.model.model_fields)


@_cases
def test_order_by_columns_are_real_model_fields(case):
    fields = set(case.model.model_fields)
    assert set(case.order_by) <= fields, f"{case.label}: order_by references non-field columns"


@_cases
def test_cohort_policy_columns_are_real_model_fields(case):
    # A typo'd cohort column would silently never suppress — a governance bug —
    # so every column the policy names must be a real field on the model.
    fields = set(case.model.model_fields)
    policy = case.model.cohort_policy
    assert policy.primary in fields, f"{case.label}: primary cohort is not a model field"
    assert set(policy.secondary) <= fields, f"{case.label}: secondary cohort not in model fields"
    for derived, cohorts in policy.derived.items():
        assert derived in fields, f"{case.label}: derived column {derived!r} not in model fields"
        assert set(cohorts) <= fields, f"{case.label}: {derived!r} references a missing cohort"
    # Same argument for the containment declarations: a typo'd container is a
    # complement that never gets checked.
    for subset, container in policy.contained_in.items():
        assert subset in fields, f"{case.label}: contained cohort {subset!r} not a model field"
        assert container in fields, f"{case.label}: container {container!r} not a model field"
    assert set(policy.uncontained) <= fields, f"{case.label}: uncontained names a missing column"


def test_finer_grain_declarations_match_the_contract_endpoint_they_name():
    """The cross-grain guard is only sound if the coarse endpoint and the
    contract endpoint it probes really are the same data at two grains."""
    contract_models = {spec.mv: spec.model for spec in contracts.ENDPOINTS}

    for spec in organizations.ENDPOINTS:
        if spec.finer_grain is None:
            continue
        finer_model = contract_models[spec.finer_grain.mv]
        coarse_fields = set(spec.model.model_fields)
        # The probe filters on the key and the coarse rows are matched by it,
        # so both grains have to carry it.
        (key_column,) = spec.order_by
        assert key_column in coarse_fields
        assert key_column in set(finer_model.model_fields)
        # The probe compares against the finer grain's own row gate, so it
        # withholds exactly the keys suppress_small_cohorts drops there.
        assert spec.finer_grain.cohort_column == finer_model.cohort_policy.primary
        # Only event sums add up exactly across contracts. Blanking a cohort
        # count here would over-suppress; leaving out a sum would leave the
        # subtraction open.
        policy = spec.model.cohort_policy
        additives = set(spec.finer_grain.additive_columns)
        assert additives <= set(policy.derived), f"{spec.mv}: additive column is not a derived sum"
        assert not additives & {policy.primary, *policy.secondary}


@_cases
def test_projected_columns_are_all_safe_identifiers(case):
    # The whole point of centralizing splicing in build_select: everything it
    # interpolates is a validated identifier, nothing free-form.
    for column in _projected_columns(case.query):
        assert validate_sql_identifier(column) == column


def test_build_select_rejects_empty_order_by():
    # An empty order_by would emit "ORDER BY  LIMIT ..." — a SQL syntax
    # error — and deterministic pagination requires an ordering anyway.
    with pytest.raises(ValueError, match="order_by must contain at least one column"):
        build_select(
            "b2b_analytics",
            "some_mv",
            MitAdminContractHealth,
            order_by=(),
        )
