"""Org-manager check via a round-trip to MITx Online.

Phase 1 auth design (hq#10594): the Keycloak `organization` claim in
X-Userinfo carries org *membership* only, not the manager role — that flag
lives solely in MITx Online's local DB (b2b.models.UserOrganization.is_manager)
and never reaches the header. So confirming "is this user a manager of this
org" requires calling MITx Online's own manager-scoped endpoint, which
already filters to orgs the caller manages:

    GET /api/v0/b2b/manager/organizations/  (b2b/views/v0/manager.py,
    ManagerOrganizationViewSet.get_queryset filters on
    organization_users__is_manager=True for the authenticated user)

This service forwards the original request's X-Userinfo header verbatim so
MITx Online authenticates the call as the same user (mitol-django-authentication
reads X-Userinfo the same way this service does). Results are cached briefly
per (sub, org_slug) to bound added latency on every analytics request.
"""

from __future__ import annotations

import httpx
from cachetools import TTLCache

from ol_analytics_api.config import settings

_cache: TTLCache[tuple[str, str], bool] = TTLCache(
    maxsize=10_000, ttl=settings.org_manager_cache_ttl_seconds
)


class MITxOnlineClient:
    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.mitxonline_api_base_url).rstrip("/")

    async def is_org_manager(self, sub: str, org_slug: str, userinfo_header: str) -> bool:
        cache_key = (sub, org_slug)
        if cache_key in _cache:
            return _cache[cache_key]

        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.mitxonline_manager_check_timeout_seconds,
        ) as client:
            response = await client.get(
                "/api/v0/b2b/manager/organizations/",
                headers={"X-Userinfo": userinfo_header},
            )
        response.raise_for_status()
        managed_orgs = response.json()
        is_manager = any(org.get("slug") == org_slug for org in managed_orgs)

        _cache[cache_key] = is_manager
        return is_manager


mitxonline_client = MITxOnlineClient()
