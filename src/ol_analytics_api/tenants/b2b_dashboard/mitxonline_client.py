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

from ol_analytics_api.tenants.b2b_dashboard.config import settings

_cache: TTLCache[tuple[str, str], bool] = TTLCache(
    maxsize=10_000, ttl=settings.org_manager_cache_ttl_seconds
)


class MITxOnlineClient:
    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.mitxonline_api_base_url).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def start(self) -> None:
        """Open the shared connection pool. Call once, at app startup —
        see tenants/b2b_dashboard/app.py's lifespan. Reusing one client
        across calls avoids a fresh TCP+TLS handshake to MITx Online on
        every cache miss."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.mitxonline_manager_check_timeout_seconds,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            msg = "MITxOnlineClient.start() must be called before use"
            raise RuntimeError(msg)
        return self._client

    async def is_org_manager(self, sub: str, org_slug: str, userinfo_header: str) -> bool:
        cache_key = (sub, org_slug)
        if cache_key in _cache:
            return _cache[cache_key]

        response = await self._require_client().get(
            "/api/v0/b2b/manager/organizations/",
            headers={"X-Userinfo": userinfo_header},
        )
        response.raise_for_status()
        managed_orgs = response.json()
        is_manager = any(org.get("slug") == org_slug for org in managed_orgs)

        _cache[cache_key] = is_manager
        return is_manager

    async def check_reachable(self) -> None:
        """Readiness check — is MITx Online reachable at all? Deliberately
        does not exercise the manager-scoped endpoint (that's a per-user
        authorization outcome, not a fit for a pod-level readiness check);
        any HTTP response (even a 4xx/5xx) means the network path and TLS
        handshake work, so only a connection-level failure raises."""
        await self._require_client().get("/")


mitxonline_client = MITxOnlineClient()
