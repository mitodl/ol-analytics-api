"""Org-manager-scoped analytics endpoints.

Each of the 5 org-facing MVs is exposed as one GET, gated by the tenant's
require_org_manager dependency, suppressed below the anonymization floor,
and wrapped in the shared OrgAnalyticsResponse envelope (organization_key +
as_of + data) — see the Analytics API Endpoints epic.

The five endpoints are near-identical, so they're declared as a table of
``_OrgEndpoint`` specs and registered in a loop rather than hand-written five
times: the model<->MV<->ordering wiring is auditable at a glance, and the one
identifier-splicing construct lives once in ``build_select`` (no per-endpoint
``# noqa: S608``). Adding an org endpoint is one more row in ``ENDPOINTS``.

Mounted at /api/v1/analytics by tenants/b2b_dashboard/app.py — paths here
are relative to that mount point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import SQLModel

from ol_analytics_api.core.anonymization import (
    CrossGrainAdditives,
    hidden_additive_columns,
)
from ol_analytics_api.core.db.query import (
    build_count,
    build_grain_scan,
    build_select,
    cohort_policy_of,
    fetch_and_suppress,
    fetch_grain_scan,
    fetch_visible_count,
)
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_dashboard.auth import require_org_manager
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import (
    ContentEngagementDepth,
    ContractMonthlyEngagementTrend,
    ContractUtilization,
    EnrollmentCompletionFunnel,
    MonthlyEngagementTrend,
    OrgAnalyticsResponse,
    ProgramFunnel,
)
from ol_analytics_api.tenants.b2b_dashboard.pagination import Pagination, pagination

router = APIRouter(
    prefix="/organizations/{organization_id}",
    tags=["organizations"],
    dependencies=[Depends(require_org_manager)],
)

# Validated as a safe SQL identifier at settings-load time (B2BDashboardSettings
# field_validator); build_select re-validates it and every other spliced token.
_SCHEMA = settings.starrocks_schema

# The path's {organization_id} is the Keycloak organization UUID
# (sso_organization_id) -- the one identifier stable across the JWT, MITx
# Online, and StarRocks. Every org query filters to the caller's org via a
# bound param, never a spliced value -- the UUID reaches StarRocks as %s.
_ORG_FILTER_COLUMN = "sso_organization_id"


# A finer-grain scan is a guard, not paging: it has to see every finer row at
# once. This bounds what one request can pull into the pod anyway. It is sized
# far above the real shape of the data (an org's contracts times its months),
# and a scan that reaches it is treated as truncated rather than complete.
_GRAIN_SCAN_LIMIT = 10_000


@dataclass(frozen=True)
class _FinerGrain:
    """The contract-grained sibling MV whose rows sum into this endpoint's.

    Only the engagement trend needs one, and the other four org endpoints need
    none for two different reasons. Three of them (contract-utilization,
    enrollment-funnel, program-funnel) carry a contract per row already, so
    there is no coarser total to difference against. The fourth,
    content-engagement, is org x course_run against a contract-grained sibling
    of org x contract x course_run — but a course run belongs to exactly one
    contract, so the sibling adds a label rather than splitting a row, the two
    hold identical counts, and the floor makes the same call on both.

    That leaves the trend. It is the one org endpoint that aggregates *across*
    an org's contracts while the contract router publishes the same months one
    contract at a time, so what a contract-month withholds is recoverable as
    ``org_total - sum(the visible contract months)``.

    ``additive_columns`` are the event sums, which do add up exactly across
    contracts. ``non_additive_columns`` are the derived columns that do not, and
    are listed rather than left implicit: between them the two must account for
    every derived column on the coarse model, so adding a sixth aggregate to the
    MV cannot leave a new subtraction open just because nobody thought about it
    here.

    The coarse model's ``primary`` and ``secondary`` learner counts get no
    equivalent declaration here — ``_register`` guards all of them together,
    unconditionally, via ``CrossGrainAdditives.guarded_cohorts``. They are not
    exactly additive across contracts in general (a learner active under two
    is counted once at the org grain but in both contract rows), but two
    contracts sharing no learners sum exactly, and this service has no way to
    tell that case from an overlapping one before deciding what to publish. So
    every cohort column at the coarse grain is blanked whenever the finer scan
    hides anything at all for that key, not only the columns known to be safe
    from it.
    """

    mv: str
    model: type[SQLModel]
    additive_columns: tuple[str, ...]
    non_additive_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class _OrgEndpoint:
    """One org-scoped MV endpoint, declared instead of hand-written.

    ``order_by`` is a hardcoded column tuple per endpoint (never caller input)
    so LIMIT/OFFSET paging is deterministic; each column is validated as an
    identifier by ``build_select``.
    """

    path: str
    mv: str
    model: type[SQLModel]
    order_by: tuple[str, ...]
    finer_grain: _FinerGrain | None = None

    def __post_init__(self) -> None:
        """Reject a finer-grain declaration that leaves a derived column
        unaccounted for.

        Same argument as ``CohortPolicy``'s own validation, which this mirrors:
        the failure being guarded against is a column nobody classified quietly
        keeping its subtraction open, and no test would notice. Failing at
        import time makes it impossible to deploy.
        """
        if self.finer_grain is None:
            return
        derived = set(cohort_policy_of(self.model).derived)
        additive = set(self.finer_grain.additive_columns)
        non_additive = set(self.finer_grain.non_additive_columns)
        if overlap := additive & non_additive:
            msg = f"{self.path}: {sorted(overlap)} are both additive and non-additive."
            raise ValueError(msg)
        if unknown := (additive | non_additive) - derived:
            msg = (
                f"{self.path}: {sorted(unknown)} are not derived columns of "
                f"{self.model.__name__}, so nothing sums into them."
            )
            raise ValueError(msg)
        if unclassified := derived - additive - non_additive:
            msg = (
                f"{self.path}: derived columns {sorted(unclassified)} are classified "
                "neither additive nor non-additive across the finer grain. An "
                "exactly-additive column left out keeps its subtraction open."
            )
            raise ValueError(msg)


ENDPOINTS: list[_OrgEndpoint] = [
    _OrgEndpoint(
        "/contract-utilization",
        "mv_b2b_contract_utilization",
        ContractUtilization,
        ("contract_pk",),
    ),
    _OrgEndpoint(
        "/enrollment-funnel",
        "mv_b2b_enrollment_completion_funnel",
        EnrollmentCompletionFunnel,
        ("contract_pk", "courserun_pk"),
    ),
    _OrgEndpoint(
        "/engagement-trend",
        "mv_b2b_monthly_engagement_trend",
        MonthlyEngagementTrend,
        ("activity_year_and_month",),
        finer_grain=_FinerGrain(
            "mv_b2b_contract_monthly_engagement_trend",
            ContractMonthlyEngagementTrend,
            additive_columns=(
                "new_enrollments",
                "certificates_earned",
                "total_videos_watched",
                "total_problems_attempted",
                "total_chatbot_interactions",
            ),
        ),
    ),
    _OrgEndpoint(
        "/program-funnel",
        "mv_b2b_program_funnel",
        ProgramFunnel,
        ("contract_pk", "program_pk"),
    ),
    _OrgEndpoint(
        "/content-engagement",
        "mv_b2b_content_engagement_depth",
        ContentEngagementDepth,
        ("courserun_readable_id",),
    ),
]


def _register(spec: _OrgEndpoint) -> None:
    """Register one org endpoint: run its MV query -> suppress -> wrap in the
    org envelope. Pagination bounds the row count so a large org can't load its
    whole grain into the pod; suppression then runs over that bounded page.
    ``as_of`` is the backing MV's own last-refresh time, so the freshness a
    manager sees always matches the data in that panel — a different MV lagging
    its refresh can't make this endpoint look fresher than it is.

    Which columns the anonymization floor applies to is the row model's own
    ``cohort_policy`` — ``fetch_and_suppress`` reads it, so the spec names only
    the model, not a cohort field.

    ``total_count`` costs one more, cheap COUNT(*) against the same MV. It is
    what lets a client say "showing 200 of 340" rather than truncating at the
    page cap with nothing to show for it — worth a second round trip on an
    endpoint whose data only changes when the MV refreshes, hours apart.

    An endpoint declaring a ``finer_grain`` pays for one more round trip, and
    only that endpoint: a scan of its contract-grained sibling, suppressed here
    exactly as that sibling's own endpoint would suppress it, so the additive
    columns it does not publish in full — and, more bluntly, every cohort
    column at this grain — can be blanked for whatever key it hid something
    for.
    """
    query = build_select(
        _SCHEMA, spec.mv, spec.model, filter_columns=(_ORG_FILTER_COLUMN,), order_by=spec.order_by
    )
    count_query = build_count(_SCHEMA, spec.mv, spec.model, filter_columns=(_ORG_FILTER_COLUMN,))
    additives = scan_query = None
    if spec.finer_grain is not None:
        # The org grain's ordering column is also what lines an org row up with
        # the contract rows summing into it, so the same tuple names the guard's
        # key. Endpoints with a finer grain are single-keyed by construction.
        (key_column,) = spec.order_by
        coarse_policy = cohort_policy_of(spec.model)
        additives = CrossGrainAdditives(
            key_column,
            spec.finer_grain.additive_columns,
            guarded_cohorts=(coarse_policy.primary, *coarse_policy.secondary),
        )
        scan_query = build_grain_scan(
            _SCHEMA,
            spec.finer_grain.mv,
            spec.finer_grain.model,
            filter_columns=(_ORG_FILTER_COLUMN,),
        )

    async def endpoint(
        organization_id: str, page: Annotated[Pagination, Depends(pagination)]
    ) -> OrgAnalyticsResponse[SQLModel]:
        cross_grain = None
        if spec.finer_grain is not None and additives is not None and scan_query is not None:
            # Scanned across the whole org, not just this page: an org row on
            # page 1 can be reconstructed from contract rows the caller reads
            # in any page of the contract endpoint, so the page boundary is
            # not a limit on what they can subtract.
            finer_rows = await fetch_grain_scan(scan_query, (organization_id, _GRAIN_SCAN_LIMIT))
            if len(finer_rows) >= _GRAIN_SCAN_LIMIT:
                # Truncated, so the guard cannot prove it saw every contributing
                # row. Blanking every additive column for every key is the only
                # answer that stays correct; a partial scan silently publishing
                # a total it could not check is the failure this whole guard is.
                msg = (
                    f"{spec.mv}: finer-grain scan hit its {_GRAIN_SCAN_LIMIT}-row limit "
                    "for one organization, so the cross-grain guard cannot be applied."
                )
                raise RuntimeError(msg)
            cross_grain = (
                additives,
                hidden_additive_columns(
                    finer_rows,
                    cohort_policy_of(spec.finer_grain.model),
                    settings.anonymization_floor,
                    key_column=key_column,
                    additive_columns=spec.finer_grain.additive_columns,
                ),
            )
        rows = await fetch_and_suppress(
            query,
            (organization_id, page.limit, page.offset),
            spec.model,
            settings.anonymization_floor,
            cross_grain=cross_grain,
        )
        return OrgAnalyticsResponse(
            organization_id=organization_id,
            as_of=await latest_refresh_timestamp(_SCHEMA, spec.mv),
            total_count=await fetch_visible_count(
                count_query, (organization_id, settings.anonymization_floor)
            ),
            data=rows,
        )

    endpoint.__name__ = spec.path.lstrip("/").replace("-", "_")
    router.add_api_route(
        spec.path,
        endpoint,
        methods=["GET"],
        # Runtime-parametrized generic so each endpoint's OpenAPI schema carries
        # its concrete row model; mypy can't type a value used as a type param.
        response_model=OrgAnalyticsResponse[spec.model],  # type: ignore[name-defined]
        name=endpoint.__name__,
        # Named explicitly because this is what a generated client's method is
        # called. FastAPI's default derives one from the function name *and*
        # the whole path, which would make the TS method
        # `contractUtilizationOrganizationsOrganizationIdContractUtilizationGet`
        # and — worse — churn it whenever the path changes. The tag prefix is
        # what keeps this distinct from the contract router's same-named panel.
        operation_id=f"organizations_{endpoint.__name__}_retrieve",
    )


for _spec in ENDPOINTS:
    _register(_spec)
