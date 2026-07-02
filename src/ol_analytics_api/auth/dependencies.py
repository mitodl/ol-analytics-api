"""FastAPI auth dependencies — org-manager and MIT-admin gates.

See auth/mitxonline_client.py and auth/keycloak.py for the underlying checks.
Phase 1 design per hq#10594: org-manager requires a round-trip to MITx
Online (membership alone isn't sufficient); MIT-admin is a Keycloak realm
role and needs no round-trip.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from ol_analytics_api.auth.keycloak import get_userinfo
from ol_analytics_api.auth.mitxonline_client import mitxonline_client
from ol_analytics_api.config import settings

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
