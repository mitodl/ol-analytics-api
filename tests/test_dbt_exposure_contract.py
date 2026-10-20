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

The hand-written queries (b2b_learner_records, and the dashboard's
learner_queries.py behind the learner and needs-attention routers) get the
first check only. sqlglot parses each variant of each query and resolves every
column reference through its subqueries, CTEs and joins to a view column, so a
column dbt drops or renames fails here rather than at request time. The views
those queries read declare no cohorts, so there is no policy to compare.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify
from sqlmodel import SQLModel

from ol_analytics_api.core.anonymization import CohortPolicy
from ol_analytics_api.tenants.b2b_dashboard import auth, learner_queries
from ol_analytics_api.tenants.b2b_dashboard.config import settings as dashboard_settings
from ol_analytics_api.tenants.b2b_dashboard.models import MitAdminContractHealth
from ol_analytics_api.tenants.b2b_dashboard.routers import admin, contracts, organizations
from ol_analytics_api.tenants.b2b_learner_records import queries as learner_records

_EXPOSURES_DIR = Path(__file__).parent / "dbt_exposures"


def _load(exposure: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Return ``{view: {column: column entry}}`` for one exposure's contract file."""
    document = json.loads((_EXPOSURES_DIR / f"{exposure}.json").read_text())
    return {
        view: {column["name"]: column for column in model["columns"]}
        for view, model in document["models"].items()
    }


_DASHBOARD = _load("ol_analytics_api_b2b_dashboard")
_LEARNER_RECORDS = _load("ol_analytics_api_b2b_learner_records")


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


# --- hand-written queries -------------------------------------------------
#
# Each case is one variant of one query: the filters choose which predicates,
# joins and branches the text contains, so the variants together reach every
# column reference. Setting a filter only adds references, except where a flag
# switches between branches (include_inactive, and contract_id on the learner
# rollup), which get a case per branch.

_LR_SCHEMA = "lr"
_ORG = uuid.UUID(int=1)
_INSTANT = datetime.datetime(2026, 1, 1, 12, 30, 15, 500, tzinfo=datetime.UTC)
_TODAY = datetime.date(2026, 1, 1)
_EVERY_RECORD_FILTER = learner_records.RecordFilters(
    _ORG,
    contract_id=1,
    contract_is_active=True,
    courserun_id="course-v1:MITx+1+1T2026",
    courserun_starts_after=_INSTANT,
    courserun_starts_before=_INSTANT,
    learner_ids=(_ORG,),
    completion_statuses=("passed", "unknown"),
    updated_since=_INSTANT,
    updated_before=_INSTANT,
)
_EVERY_PROGRESS_FILTER = learner_queries.ProgressFilters(
    "org",
    1,
    search="a",
    completion_statuses=("passed", "unknown"),
    courserun_readable_id="course-v1:MITx+1+1T2026",
    needs_attention=True,
)


@dataclass(frozen=True)
class _Query:
    label: str
    exposure: dict[str, dict[str, dict[str, Any]]]
    schema: str
    build: Callable[[], Any]


_HAND_WRITTEN = [
    _Query(
        "learner_records.learners",
        _LEARNER_RECORDS,
        _LR_SCHEMA,
        lambda: learner_records.learners(_LR_SCHEMA, learner_records.RecordFilters(_ORG)),
    ),
    *(
        _Query(
            f"learner_records.learners (contract_id={contract_id}, "
            f"include_inactive={include_inactive})",
            _LEARNER_RECORDS,
            _LR_SCHEMA,
            lambda contract_id=contract_id, include_inactive=include_inactive: (
                learner_records.learners(
                    _LR_SCHEMA,
                    learner_records.RecordFilters(
                        _ORG, contract_id=contract_id, include_inactive=include_inactive
                    ),
                )
            ),
        )
        for contract_id, include_inactive in ((1, False), (None, True), (1, True))
    ),
    *(
        _Query(
            f"learner_records.enrollments (include_inactive={include_inactive})",
            _LEARNER_RECORDS,
            _LR_SCHEMA,
            lambda include_inactive=include_inactive: learner_records.enrollments(
                _LR_SCHEMA,
                learner_records.RecordFilters(
                    **{**vars(_EVERY_RECORD_FILTER), "include_inactive": include_inactive}
                ),
            ),
        )
        for include_inactive in (False, True)
    ),
    _Query(
        "learner_records.courses",
        _LEARNER_RECORDS,
        _LR_SCHEMA,
        lambda: learner_records.courses(_LR_SCHEMA, _EVERY_RECORD_FILTER),
    ),
    *(
        _Query(
            f"learner_queries.learner_progress (sort={sort.value}, "
            f"include_inactive={include_inactive})",
            _DASHBOARD,
            dashboard_settings.learner_records_schema,
            lambda sort=sort, include_inactive=include_inactive: learner_queries.learner_progress(
                learner_queries.ProgressFilters(
                    **{
                        **vars(_EVERY_PROGRESS_FILTER),
                        "sort": sort,
                        "include_inactive": include_inactive,
                    }
                ),
                _TODAY,
            ),
        )
        for sort in learner_queries.SortKey
        for include_inactive in (False, True)
    ),
    _Query(
        "learner_queries.learners",
        _DASHBOARD,
        dashboard_settings.learner_records_schema,
        lambda: learner_queries.learners(
            learner_queries.LearnerFilters(
                "org",
                1,
                search="a",
                completion_statuses=("passed", "unknown"),
                courserun_readable_ids=("course-v1:MITx+1+1T2026", "course-v1:MITx+2+1T2026"),
                needs_attention=False,
            ),
            _TODAY,
        ),
    ),
    *(
        _Query(
            f"learner_queries.needs_attention_aggregate (contract_id={contract_id})",
            _DASHBOARD,
            dashboard_settings.learner_records_schema,
            lambda contract_id=contract_id: learner_queries.needs_attention_aggregate(
                "org", contract_id, _TODAY
            ),
        )
        for contract_id in (None, 1)
    ),
    _Query(
        "learner_queries.course_runs",
        _DASHBOARD,
        dashboard_settings.learner_records_schema,
        lambda: learner_queries.course_runs("org", 1),
    ),
]

_STATEMENTS = [
    pytest.param(query, statement, id=f"{query.label} {statement}")
    for query in _HAND_WRITTEN
    for statement in ("page", "count")
]


def _sqlglot_schema(query: _Query, *, without: tuple[str, str] | None = None) -> dict[str, Any]:
    """The exposure as a sqlglot schema under the query's schema name.

    The type is a placeholder: column resolution does not read it.
    """
    return {
        query.schema: {
            view: {column: "VARCHAR" for column in columns if (view, column) != without}
            for view, columns in query.exposure.items()
        }
    }


def _parse(sql: str) -> exp.Expression:
    # The driver's %s placeholders are not SQL; ? is the placeholder sqlglot reads.
    return sqlglot.parse_one(sql.replace("%s", "?"), read="starrocks")


def _resolve_columns(tree: exp.Expression, schema: dict[str, Any]) -> None:
    """Raise OptimizeError naming the first column no view or alias provides."""
    qualify(tree, schema=schema, dialect="starrocks", validate_qualify_columns=True)


@pytest.mark.parametrize(("query", "statement"), _STATEMENTS)
def test_every_column_a_hand_written_query_reads_exists_in_the_view(query, statement):
    tree = _parse(getattr(query.build(), statement))
    views = {table.name for table in tree.find_all(exp.Table) if table.db == query.schema}
    assert views, f"{query.label}: reads no view in schema {query.schema}"
    unknown = sorted(views - set(query.exposure))
    assert not unknown, f"{query.label}: {unknown} are not views the exposure depends on"
    try:
        _resolve_columns(tree, _sqlglot_schema(query))
    except OptimizeError as error:
        pytest.fail(f"{query.label} {statement}: {error}")


def test_a_column_missing_from_the_view_fails_the_hand_written_check():
    # Guards the check itself: if a sqlglot upgrade stopped validating
    # references, the test above would pass for every query.
    (query,) = [
        q
        for q in _HAND_WRITTEN
        if q.label == "learner_records.enrollments (include_inactive=False)"
    ]
    tree = _parse(query.build().page)
    with pytest.raises(OptimizeError, match="videos_played"):
        _resolve_columns(
            tree, _sqlglot_schema(query, without=(learner_records.ENROLLMENT_MV, "videos_played"))
        )
