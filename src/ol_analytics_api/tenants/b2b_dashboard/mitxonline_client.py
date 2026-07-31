"""Org-manager check via a service-authenticated round-trip to MITx Online.

The `is_manager` flag lives solely in MITx Online's local DB
(b2b.models.UserOrganization.is_manager, curated in their Django admin). The
Keycloak `organization` claim in X-Userinfo carries org *membership* only, so
confirming "is this user a manager of this org" requires asking MITx Online.

This service authenticates with its own OAuth2 client-credentials token and
names the subject user explicitly:

    GET /api/v0/b2b/service/organization-manager-check/
        ?sso_organization_id=<org uuid>&user_global_id=<keycloak sub>
    -> {"is_manager": bool}

That endpoint exists (mitodl/mitxonline#3807) because the two more obvious
approaches cannot work:

* Forwarding the caller's X-Userinfo to MITx Online's own manager endpoint.
  The APISIX openid-connect plugin deliberately strips client-supplied
  X-Userinfo/X-Access-Token headers before they reach an upstream, so the
  forwarded identity never survives the gateway and MITx Online sees an
  anonymous request. This is what made every analytics request 502.
* Calling MITx Online's existing manager endpoint with a service token. That
  endpoint's queryset filters on the *authenticated* user, so a service
  credential would answer "manages nothing" for everybody.

`user_global_id` is the Keycloak `sub`: MITx Online resolves users by
`global_id` (MITOL_APIGATEWAY_USERINFO_ID_SEARCH_FIELD), which holds exactly
that value.

All of this is temporary. Once org-manager status is visible in the Keycloak
token (mitodl/hq#10594) the round-trip disappears and this module goes with
it.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import structlog
from cachetools import TTLCache

from ol_analytics_api.tenants.b2b_dashboard.config import settings

log = structlog.get_logger(__name__)

_cache: TTLCache[tuple[str, str], bool] = TTLCache(
    maxsize=10_000, ttl=settings.org_manager_cache_ttl_seconds
)

# Refresh a token this many seconds before it actually expires, so a request
# that starts just under the wire doesn't arrive with a token that died in
# flight.
_TOKEN_EXPIRY_MARGIN_SECONDS = 30.0


class MITxOnlineClient:
    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.mitxonline_api_base_url).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # Serializes token minting. Without it, N concurrent cache misses on a
        # cold or just-expired token would each open their own token request.
        self._token_lock = asyncio.Lock()

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
        self._token = None
        self._token_expires_at = 0.0

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            msg = "MITxOnlineClient.start() must be called before use"
            raise RuntimeError(msg)
        return self._client

    async def _fetch_token(self) -> str:
        """Mint a fresh client-credentials access token."""
        response = await self._require_client().post(
            "/oauth2/token/",
            data={
                "grant_type": "client_credentials",
                "scope": settings.mitxonline_oauth_scope,
            },
            auth=(settings.mitxonline_client_id, settings.mitxonline_client_secret),
        )
        response.raise_for_status()
        payload = response.json()
        raw_token = payload.get("access_token") if isinstance(payload, dict) else None
        if not raw_token or not isinstance(raw_token, str):
            msg = "MITx Online token response contained no access_token"
            raise RuntimeError(msg)
        token: str = raw_token
        # expires_in is in seconds. If it's missing, treat the token as
        # single-use rather than caching it forever against an unknown
        # lifetime -- correctness beats saving a round-trip.
        expires_in = float(payload.get("expires_in") or 0)
        self._token = token
        self._token_expires_at = (
            time.monotonic() + max(0.0, expires_in - _TOKEN_EXPIRY_MARGIN_SECONDS)
            if expires_in
            else 0.0
        )
        return token

    def _token_is_fresh(self) -> bool:
        return self._token is not None and time.monotonic() < self._token_expires_at

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable access token, minting one if needed."""
        if not force_refresh and self._token_is_fresh():
            return self._token  # type: ignore[return-value]

        async with self._token_lock:
            # Re-check inside the lock: whoever held it may have just
            # refreshed, in which case this caller doesn't need to.
            if not force_refresh and self._token_is_fresh():
                return self._token  # type: ignore[return-value]
            return await self._fetch_token()

    async def _request_manager_check(
        self, sub: str, organization_id: str, *, force_token_refresh: bool = False
    ) -> httpx.Response:
        token = await self._access_token(force_refresh=force_token_refresh)
        return await self._require_client().get(
            "/api/v0/b2b/service/organization-manager-check/",
            params={"sso_organization_id": organization_id, "user_global_id": sub},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def is_org_manager(self, sub: str, organization_id: str) -> bool:
        cache_key = (sub, organization_id)
        if cache_key in _cache:
            return _cache[cache_key]

        response = await self._request_manager_check(sub, organization_id)

        # A 401 means the token was rejected — most likely revoked, or expired
        # earlier than the expires_in it advertised. Mint a fresh one and retry
        # once; a second 401 is a real misconfiguration and propagates.
        if response.status_code == httpx.codes.UNAUTHORIZED:
            log.info("MITx Online rejected the service token; refreshing and retrying")
            response = await self._request_manager_check(
                sub, organization_id, force_token_refresh=True
            )

        response.raise_for_status()
        payload = response.json()

        is_manager = payload.get("is_manager") if isinstance(payload, dict) else None
        if not isinstance(is_manager, bool):
            # Unexpected upstream shape — fail closed. Not cached: an error
            # condition, not a real authorization answer, so a transient glitch
            # shouldn't lock a legitimate manager out for the rest of the TTL.
            log.warning(
                "Unexpected org-manager check payload from MITx Online",
                payload_type=type(payload).__name__,
            )
            return False

        _cache[cache_key] = is_manager
        return is_manager

    async def check_reachable(self) -> None:
        """Readiness check — is MITx Online reachable at all? Deliberately
        does not exercise the manager-check endpoint (that's a per-user
        authorization outcome, not a fit for a pod-level readiness check) and
        deliberately does not mint a token (a credential problem shouldn't
        pull every pod out of the load balancer); any HTTP response (even a
        4xx/5xx) means the network path and TLS handshake work, so only a
        connection-level failure raises."""
        await self._require_client().get("/")


mitxonline_client = MITxOnlineClient()
