"""This tenant's governance gates — org-manager and MIT-admin.

Generic X-Userinfo decode lives in core/auth/userinfo.py, reused by any
tenant behind the same APISIX/Keycloak trust boundary. Everything below is
b2b_dashboard-specific policy: Phase 1 design per hq#10594, org-manager
requires a round-trip to MITx Online (membership alone isn't sufficient);
MIT-admin is a Keycloak realm role and needs no round-trip. A different
tenant is free to define entirely different checks (API keys, a different
realm role, no auth at all for a public tenant) in its own auth.py.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, Request, status

from ol_analytics_api.core.auth.userinfo import get_userinfo
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import mitxonline_client

UserInfo = Annotated[dict[str, Any], Depends(get_userinfo)]


async def require_org_manager(
    organization_id: str,
    request: Request,
    userinfo: UserInfo,
) -> dict[str, Any]:
    # `organization_id` is the Keycloak organization UUID (sso_organization_id) --
    # the one identifier stable across the JWT, MITx Online, and StarRocks.
    # Membership: the JWT `organization` claim is keyed by org *alias*, but each
    # value carries the org UUID via `id` (addOrganizationId mapper), so match on
    # the value's id rather than the dict key.
    orgs = userinfo.get("organization", {})
    if not any(isinstance(org, dict) and org.get("id") == organization_id for org in orgs.values()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not a member of organization '{organization_id}'",
        )

    sub = userinfo.get("sub")
    raw_header = request.headers.get("X-Userinfo", "")
    try:
        is_manager = sub is not None and await mitxonline_client.is_org_manager(
            sub, organization_id, raw_header
        )
    except httpx.HTTPStatusError as exc:
        # MITx Online responded with an error status — a real upstream
        # problem (misconfigured client credentials, a 500 on their side),
        # not a network outage, so it gets its own status rather than being
        # folded into "unreachable".
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


def require_mit_admin(userinfo: UserInfo) -> dict[str, Any]:
    roles = userinfo.get("realm_access", {}).get("roles", [])
    if settings.mit_admin_realm_role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MIT contract admin role required",
        )
    return userinfo
