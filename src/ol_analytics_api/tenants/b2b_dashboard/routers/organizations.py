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

from ol_analytics_api.core.anonymization import CrossGrainAdditives
from ol_analytics_api.core.db.query import (
    build_count,
    build_hidden_grain_probe,
    build_select,
    fetch_and_suppress,
    fetch_hidden_grain_keys,
    fetch_visible_count,
)
from ol_analytics_api.core.db.refresh_metadata import latest_refresh_timestamp
from ol_analytics_api.tenants.b2b_dashboard.auth import require_org_manager
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import (
    ContentEngagementDepth,
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


@dataclass(frozen=True)
class _FinerGrain:
    """The contract-grained sibling MV whose rows sum into this endpoint's.

    Only the engagement trend needs one. It is the single org endpoint that
    aggregates *across* an org's contracts while the contract router publishes
    the same months one contract at a time, so a contract-month the floor
    withholds is recoverable as ``org_total - sum(the visible contract
    months)``. The other four org endpoints carry a contract per row already,
    and the content-engagement pair partitions by course run — a run belongs to
    exactly one contract, so its org row and its contract row hold identical
    counts and the floor makes the same call on both. Nothing is left over to
    subtract in either case.

    ``additive_columns`` are the event sums, which do add up exactly across
    contracts. The learner counts do not — a learner active under two contracts
    is counted in both rows — so subtracting them bounds the withheld cohort
    rather than revealing it, and they stay published.
    """

    mv: str
    cohort_column: str
    additive_columns: tuple[str, ...]


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
            "monthly_active_learners",
            (
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
    only that endpoint: a probe asking its contract-grained sibling which keys
    it withholds, so the additive columns that would reconstruct those rows can
    be blanked. The probe projects a grouping key and nothing else — see
    ``build_hidden_grain_probe``.
    """
    query = build_select(
        _SCHEMA, spec.mv, spec.model, filter_columns=(_ORG_FILTER_COLUMN,), order_by=spec.order_by
    )
    count_query = build_count(_SCHEMA, spec.mv, spec.model, filter_columns=(_ORG_FILTER_COLUMN,))
    additives = probe_query = None
    if spec.finer_grain is not None:
        # The org grain's ordering column is also what lines an org row up with
        # the contract rows summing into it, so the same tuple names the probe's
        # key. Endpoints with a finer grain are single-keyed by construction.
        (key_column,) = spec.order_by
        additives = CrossGrainAdditives(key_column, spec.finer_grain.additive_columns)
        probe_query = build_hidden_grain_probe(
            _SCHEMA,
            spec.finer_grain.mv,
            key_column=key_column,
            cohort_column=spec.finer_grain.cohort_column,
            filter_columns=(_ORG_FILTER_COLUMN,),
        )

    async def endpoint(
        organization_id: str, page: Annotated[Pagination, Depends(pagination)]
    ) -> OrgAnalyticsResponse[SQLModel]:
        cross_grain = None
        if additives is not None and probe_query is not None:
            # Probed across the whole org, not just this page: an org row on
            # page 1 can be reconstructed from contract rows the caller reads
            # in any page of the contract endpoint, so the page boundary is
            # not a limit on what they can subtract.
            cross_grain = (
                additives,
                await fetch_hidden_grain_keys(
                    probe_query, (organization_id, settings.anonymization_floor)
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
    )


for _spec in ENDPOINTS:
    _register(_spec)
