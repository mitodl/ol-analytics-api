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

from fastapi import Depends, HTTPException, Request, status

from ol_analytics_api.core.auth.userinfo import get_userinfo
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.mitxonline_client import mitxonline_client

UserInfo = Annotated[dict[str, Any], Depends(get_userinfo)]


async def require_org_manager(
    org_slug: str,
    request: Request,
    userinfo: UserInfo,
) -> dict[str, Any]:
    orgs = userinfo.get("organization", {})
    if org_slug not in orgs:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not a member of organization '{org_slug}'",
        )

    sub = userinfo.get("sub")
    raw_header = request.headers.get("X-Userinfo", "")
    if not sub or not await mitxonline_client.is_org_manager(sub, org_slug, raw_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not a manager of organization '{org_slug}'",
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
