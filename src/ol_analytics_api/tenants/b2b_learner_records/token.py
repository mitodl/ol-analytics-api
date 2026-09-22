"""Verify the partner's bearer token here, rather than trusting the gateway.

Every other tenant reads its claims from X-Userinfo, which APISIX rebuilds
from a token it has already validated. That is only sound for traffic that
goes through APISIX, and nothing forces it to: the pod security group admits
the whole pod subnet, and the CNI is running with network policy disabled on
both data clusters, so the existing NetworkPolicies are no-ops. Any
compromised in-cluster workload can therefore post a forged X-Userinfo
naming a scope and an organization. For aggregate k-anonymized figures that
is a tolerable risk; these records name individual learners, so this tenant
checks the signature itself and ignores X-Userinfo entirely.

The token is a Keycloak client-credentials access token from the olapps
realm, signed RS256 with a realm key published at the realm's JWKS endpoint.
Verification is local: fetch the key set, cache it, check signature, issuer,
audience and lifetime. No call to Keycloak is on the request path except the
key fetch, which happens once per TTL or once per unseen key id.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt
import structlog
from fastapi import HTTPException, Request, status
from jwt import PyJWKSet

from ol_analytics_api.tenants.b2b_learner_records.config import settings

log = structlog.get_logger(__name__)

ALGORITHMS = ["RS256"]

# One refusal for every verification failure. The caller is a machine holding
# a contract, not a person debugging a login, and naming which check failed
# tells an attacker probing with forged tokens which part they got right.
INVALID_TOKEN_DETAIL = "Invalid or missing bearer token"  # noqa: S105 - a refusal message

# Floor on how often an unknown key id may trigger a refetch. Without it, a
# stream of tokens carrying junk kids would pull the JWKS endpoint once per
# request.
_REFETCH_COOLDOWN_SECONDS = 60.0


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=INVALID_TOKEN_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


class JWKSCache:
    """The realm's signing keys, fetched on demand and held for a TTL."""

    def __init__(self) -> None:
        self._keys: PyJWKSet | None = None
        self._fetched_at = 0.0
        self._last_refetch_attempt = 0.0
        # Serializes fetches so N concurrent requests on a cold or expired
        # cache open one connection to Keycloak, not N.
        self._lock = asyncio.Lock()

    def clear(self) -> None:
        self._keys = None
        self._fetched_at = 0.0
        self._last_refetch_attempt = 0.0

    def _is_fresh(self) -> bool:
        return (
            self._keys is not None
            and time.monotonic() - self._fetched_at < settings.jwks_cache_ttl_seconds
        )

    async def _fetch(self) -> PyJWKSet:
        async with httpx.AsyncClient(timeout=settings.jwks_timeout_seconds) as client:
            response = await client.get(settings.jwks_url)
        response.raise_for_status()
        keys = PyJWKSet.from_dict(response.json())
        self._keys = keys
        self._fetched_at = time.monotonic()
        return keys

    async def _load(self, *, force: bool = False) -> PyJWKSet:
        if not force and self._is_fresh():
            return self._keys  # type: ignore[return-value]
        async with self._lock:
            # Whoever held the lock may have just fetched, in which case this
            # caller rides on their result.
            if not force and self._is_fresh():
                return self._keys  # type: ignore[return-value]
            return await self._fetch()

    async def signing_key(self, kid: str) -> jwt.PyJWK:
        """The key with this id, refetching once if it isn't in the cache.

        Keycloak rotates realm keys without warning, and the first token
        signed by a new key arrives before the TTL expires. Refetching on an
        unknown id turns that into one extra request instead of a TTL's worth
        of refusals.
        """
        keys = await self._load()
        try:
            return keys[kid]
        except KeyError:
            pass

        now = time.monotonic()
        if now - self._last_refetch_attempt < _REFETCH_COOLDOWN_SECONDS:
            raise _unauthorized() from None
        self._last_refetch_attempt = now

        log.info("Refetching JWKS for an unknown key id", kid=kid)
        keys = await self._load(force=True)
        try:
            return keys[kid]
        except KeyError as exc:
            raise _unauthorized() from exc


jwks_cache = JWKSCache()


def bearer_token(request: Request) -> str:
    """Pull the raw token out of the request.

    Authorization is where a client-credentials caller puts it and APISIX
    passes it through untouched (it only ever overwrites it with the same
    token). X-Access-Token is the gateway's own output header, and its
    openid-connect plugin accepts a bearer there too; accepting both keeps
    this service working whichever way the route is configured. Neither is
    trusted: both end up at the same signature check.
    """
    header = request.headers.get("Authorization")
    if header:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _unauthorized()
        return token.strip()
    token = request.headers.get("X-Access-Token", "")
    if not token:
        raise _unauthorized()
    return token


async def verified_claims(request: Request) -> dict[str, Any]:
    """The token's claims, or 401. This replaces get_userinfo for this tenant."""
    token = bearer_token(request)
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc
    if not isinstance(kid, str):
        # Keycloak always sets kid. Without one there is nothing to select a
        # key by, and trying every key in the set is how you end up accepting
        # a token signed by a key meant for something else.
        raise _unauthorized()

    try:
        key = await jwks_cache.signing_key(kid)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # The key set is unreachable or unusable. That is this service's
        # problem, not the caller's, but it must not open the door: refuse.
        log.warning("Could not load the realm JWKS", error=str(exc), jwks_url=settings.jwks_url)
        raise _unauthorized() from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=ALGORITHMS,
            audience=settings.audience,
            issuer=settings.issuer,
            leeway=settings.token_leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        log.info("Refused a bearer token", reason=type(exc).__name__)
        raise _unauthorized() from exc
    return claims
