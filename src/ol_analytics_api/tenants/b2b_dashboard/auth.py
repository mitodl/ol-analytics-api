"""This tenant's governance gates — org-manager and MIT-admin.

Generic X-Userinfo decode lives in core/auth/userinfo.py, reused by any
tenant behind the same APISIX/Keycloak trust boundary. Everything below is
b2b_dashboard-specific policy: per hq#10594, org-manager requires a
service-authenticated round-trip to MITx Online (membership alone isn't
sufficient, and the manager flag never reaches the token); MIT-admin is a
Keycloak realm role and needs no round-trip. A different tenant is free to
define entirely different checks (API keys, a different realm role, no auth
at all for a public tenant) in its own auth.py.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, status

from ol_analytics_api.core.auth.userinfo import get_userinfo
from ol_analytics_api.core.db.client import starrocks_pool
from ol_analytics_api.core.db.query import build_existence_check
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import mitxonline_client

UserInfo = Annotated[dict[str, Any], Depends(get_userinfo)]

# One row per contract, including contracts with no enrollments — see
# require_contract_in_org for why that property is what makes it the right
# oracle.
_CONTRACT_MEMBERSHIP_MV = "mv_b2b_contract_utilization"


async def require_org_manager(
    organization_id: str,
    userinfo: UserInfo,
) -> dict[str, Any]:
    # `organization_id` is the Keycloak organization UUID (sso_organization_id) --
    # the one identifier stable across the JWT, MITx Online, and StarRocks.
    # Membership: the JWT `organization` claim is keyed by org *alias*, but each
    # value carries the org UUID via `id` (addOrganizationId mapper), so match on
    # the value's id rather than the dict key.
    orgs = userinfo.get("organization")
    # get_userinfo only JSON-decodes the header, so `organization` may be any
    # shape (or absent). A malformed claim must fail closed (403), never 500 on
    # `.values()` -- treat anything that isn't a dict as "no memberships".
    org_values = orgs.values() if isinstance(orgs, dict) else []
    if not any(isinstance(org, dict) and org.get("id") == organization_id for org in org_values):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not a member of organization '{organization_id}'",
        )

    sub = userinfo.get("sub")
    try:
        is_manager = sub is not None and await mitxonline_client.is_org_manager(
            sub, organization_id
        )
    except httpx.HTTPStatusError as exc:
        # MITx Online responded with an error status — a real upstream
        # problem (misconfigured client credentials, a 500 on their side),
        # not a network outage, so it gets its own status rather than being
        # folded into "unreachable". Note this no longer catches "the user
        # isn't a manager": that is a 200 with is_manager=false, and falls
        # through to the 403 below.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not verify organization-manager status: MITx Online returned an error",
        ) from exc
    except httpx.RequestError as exc:
        # A network-level failure talking to MITx Online is not the caller's
        # fault — surface it as an upstream-unavailable error, not a bare 500.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify organization-manager status: MITx Online unreachable",
        ) from exc

    if not is_manager:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not a manager of organization '{organization_id}'",
        )
    return userinfo


async def require_contract_in_org(organization_id: str, contract_id: str) -> None:
    """403 unless `contract_id` belongs to `organization_id`.

    Mirrors MITx Online's IsOrganizationManager.has_object_permission, which
    authorizes at the org level and then checks the contract's ownership
    (b2b/permissions.py). Runs after require_org_manager, so reaching here
    already means the caller manages this org.

    Without it, naming another org's contract would return an empty result
    rather than a refusal — indistinguishable from a contract of your own with
    no data yet, and a weaker answer than the one MITx Online gives for the
    same request.

    mv_b2b_contract_utilization is the membership oracle because dbt builds it
    from dim_contract with a LEFT JOIN to enrollments: every contract has a row
    there, including one nobody has enrolled in. Checking any activity-derived
    view instead would 403 a real but empty contract.

    This is the one read in the tenant that does not go through
    fetch_and_suppress, and it stays outside the anonymization chokepoint
    deliberately: it selects no cohort and returns no row, only whether a
    (org, contract) pair exists — which the caller learned from the MITx Online
    dashboard that sent them here. Keep it that way; selecting a count would
    put an unfloored aggregate on an unsuppressed path.
    """
    query = build_existence_check(
        settings.starrocks_schema,
        _CONTRACT_MEMBERSHIP_MV,
        ("sso_organization_id", "contract_id"),
    )
    rows = await starrocks_pool.fetch_all(query, (organization_id, contract_id))
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Contract '{contract_id}' does not belong to organization '{organization_id}'"
            ),
        )


def require_mit_admin(userinfo: UserInfo) -> dict[str, Any]:
    roles = userinfo.get("realm_access", {}).get("roles", [])
    if settings.mit_admin_realm_role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MIT contract admin role required",
        )
    return userinfo
