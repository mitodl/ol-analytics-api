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
per (sub, organization_id) to bound added latency on every analytics request.
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

    async def is_org_manager(self, sub: str, organization_id: str, userinfo_header: str) -> bool:
        cache_key = (sub, organization_id)
        if cache_key in _cache:
            return _cache[cache_key]

        # Ask MITx Online whether this user manages the org with this Keycloak
        # UUID. The endpoint's queryset is already scoped to the caller's managed
        # orgs; the `sso_organization_id` filter narrows it to the requested one.
        # We don't trust a bare non-empty list, though -- see the match check
        # below, which keeps this fail-closed even if an older deploy ignored the
        # filter and returned all of the caller's managed orgs.
        response = await self._require_client().get(
            "/api/v0/b2b/manager/organizations/",
            params={"sso_organization_id": organization_id},
            headers={"X-Userinfo": userinfo_header},
        )
        response.raise_for_status()
        payload = response.json()
        # The manager endpoint paginates (PageNumberPagination), so the list of
        # orgs is under `results`; tolerate a bare list too.
        results = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(results, list):
            # Unexpected upstream shape (e.g. an error payload) — fail closed.
            # Not cached: an error condition, not a real authorization answer,
            # so a transient glitch shouldn't lock a legitimate manager out for
            # the rest of the cache TTL.
            return False
        # Verify a returned org actually carries the requested UUID rather than
        # trusting a non-empty list. If an older MITx Online ignored the
        # sso_organization_id filter it would return ALL of the caller's managed
        # orgs, and a bare length check would fail *open*; matching on the
        # serialized sso_organization_id makes this fail *closed* instead (an old
        # serializer without the field yields None, which never matches).
        is_manager = any(
            isinstance(org, dict) and str(org.get("sso_organization_id")) == organization_id
            for org in results
        )

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
