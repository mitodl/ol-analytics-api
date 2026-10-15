"""The dbt half of the SELECT<->model column contract, checked without a StarRocks.

ol-data-platform commits, per application exposure, the columns of every view
the exposure reads and each column's ``config.meta.cohort`` declaration
(``exposures/<name>.json``, written by ``ol-dbt exposures export``). The files
in ``tests/dbt_exposures/`` are copies of those; CI fails when they differ from
ol-data-platform main, so a dbt change to a view this service reads reaches
this test.

Two things are checked for every declarative b2b_dashboard endpoint: every
column the endpoint reads exists in the view, and the model's CohortPolicy says
what the dbt declaration next to the SQL says. The second matters because the
two are written independently, and a column one side calls a cohort while the
other calls it a measure is a floor that is not applied.

Not covered: the hand-written queries (b2b_learner_records, the dashboard's
learner and needs-attention routers). Their column references sit inside SQL
text that would have to be parsed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import SQLModel

from ol_analytics_api.core.anonymization import CohortPolicy
from ol_analytics_api.tenants.b2b_dashboard import auth
from ol_analytics_api.tenants.b2b_dashboard.models import MitAdminContractHealth
from ol_analytics_api.tenants.b2b_dashboard.routers import admin, contracts, organizations

_EXPOSURES_DIR = Path(__file__).parent / "dbt_exposures"


def _load(exposure: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Return ``{view: {column: column entry}}`` for one exposure's contract file."""
    document = json.loads((_EXPOSURES_DIR / f"{exposure}.json").read_text())
    return {
        view: {column["name"]: column for column in model["columns"]}
        for view, model in document["models"].items()
    }


_DASHBOARD = _load("ol_analytics_api_b2b_dashboard")


@dataclass(frozen=True)
class _Read:
    label: str
    mv: str
    model: type[SQLModel]
    other_columns: tuple[str, ...]


_READS = (
    [
        _Read(
            spec.mv,
            spec.mv,
            spec.model,
            (organizations._ORG_FILTER_COLUMN, *spec.order_by),  # noqa: SLF001
        )
        for spec in organizations.ENDPOINTS
    ]
    + [
        _Read(
            f"{spec.finer_grain.mv} (finer grain of {spec.mv})",
            spec.finer_grain.mv,
            spec.finer_grain.model,
            (organizations._ORG_FILTER_COLUMN,),  # noqa: SLF001
        )
        for spec in organizations.ENDPOINTS
        if spec.finer_grain is not None
    ]
    + [
        _Read(
            f"{spec.mv} (contract-scoped)",
            spec.mv,
            spec.model,
            (*contracts._FILTER_COLUMNS, *spec.order_by),  # noqa: SLF001
        )
        for spec in contracts.ENDPOINTS
    ]
    + [
        _Read(admin._MV, admin._MV, MitAdminContractHealth, admin._ORDER_BY),  # noqa: SLF001
    ]
)

_reads = pytest.mark.parametrize("read", _READS, ids=[r.label for r in _READS])


@_reads
def test_every_column_read_exists_in_the_view(read):
    assert read.mv in _DASHBOARD, f"{read.mv} is not a view the b2b_dashboard exposure depends on"
    read_columns = {*read.model.model_fields, *read.other_columns}
    missing = sorted(read_columns - set(_DASHBOARD[read.mv]))
    assert not missing, f"{read.label}: the view has no column {missing}"


def _dbt_policy(mv: str, fields: set[str]) -> dict[str, Any]:
    """The dbt declarations of ``fields``, in the shape of a CohortPolicy."""
    cohorts = {name: _DASHBOARD[mv][name]["cohort"] for name in fields}
    with_role = {
        role: {name for name, cohort in cohorts.items() if cohort["role"] == role}
        for role in ("primary", "secondary", "derived")
    }
    return {
        "primary": with_role["primary"],
        "secondary": with_role["secondary"],
        "derived": {name: set(cohorts[name]["derived_from"]) for name in with_role["derived"]},
        "contained_in": {
            name: cohort["contained_in"]
            for name, cohort in cohorts.items()
            if "contained_in" in cohort
        },
        "uncontained": {name for name, cohort in cohorts.items() if cohort.get("uncontained")},
    }


def _api_policy(policy: CohortPolicy) -> dict[str, Any]:
    return {
        "primary": {policy.primary},
        "secondary": set(policy.secondary),
        "derived": {name: set(cohorts) for name, cohorts in policy.derived.items()},
        "contained_in": dict(policy.contained_in),
        "uncontained": set(policy.uncontained),
    }


@_reads
def test_cohort_policy_matches_the_dbt_declarations(read):
    fields = set(read.model.model_fields) & set(_DASHBOARD[read.mv])
    model_name = read.model.__name__
    assert _api_policy(read.model.cohort_policy) == _dbt_policy(read.mv, fields), (
        f"{read.label}: {model_name}.cohort_policy and config.meta.cohort on {read.mv} disagree"
    )


def test_contract_membership_oracle_has_its_filter_columns():
    view = _DASHBOARD[auth._CONTRACT_MEMBERSHIP_MV]  # noqa: SLF001
    assert {"sso_organization_id", "contract_id"} <= set(view)
