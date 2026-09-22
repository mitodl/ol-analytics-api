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
audience, token class and lifetime. No call to Keycloak is on the request
path except the key fetch, which happens once per TTL or once per unseen key
id.

PyJWT ships PyJWKClient, which caches and refetches much like JWKSCache
below. It fetches with urllib, which would block the event loop on every
cache miss, so the cache here is a small async reimplementation rather than
a wrapper around it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt
import structlog
from fastapi import Request, status
from jwt import PyJWK, PyJWKSet

from ol_analytics_api.tenants.b2b_learner_records.config import settings
from ol_analytics_api.tenants.b2b_learner_records.errors import ApiError, ErrorCode

log = structlog.get_logger(__name__)

ALGORITHMS = ("RS256",)

# Keycloak's token class, in the payload. An ID token for this same client id
# carries the realm's signature, this issuer and this audience, so without
# this check it clears verification and is stopped only by carrying no
# organization grant. That is one claim deep; this closes the class.
ACCESS_TOKEN_TYPE = "Bearer"  # noqa: S105 - a claim value, not a credential

# One refusal for every verification failure. The caller is a machine holding
# a contract, not a person debugging a login, and naming which check failed
# tells an attacker probing with forged tokens which part they got right.
INVALID_TOKEN_DETAIL = "Invalid or missing bearer token"  # noqa: S105 - a refusal message

# Floor on how often an unknown key id may trigger a refetch. Without it, a
# stream of tokens carrying junk kids would pull the JWKS endpoint once per
# request.
_KID_REFETCH_COOLDOWN_SECONDS = 60.0

# How long to stop trying after a fetch fails. Without it, every request that
# arrives with the cache cold or expired and Keycloak unreachable takes the
# lock and pays the full timeout in turn, so callers queue up behind each
# other for as long as the outage lasts.
_FETCH_RETRY_COOLDOWN_SECONDS = 5.0


class JWKSUnavailableError(Exception):
    """The realm's key set could not be fetched or parsed."""


def _unauthorized() -> ApiError:
    return ApiError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code=ErrorCode.UNAUTHORIZED,
        detail=INVALID_TOKEN_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


class JWKSCache:
    """The realm's signing keys, fetched on demand and held for a TTL."""

    def __init__(self) -> None:
        self._keys: PyJWKSet | None = None
        self._fetched_at = 0.0
        self._retry_after = 0.0
        self._kid_refetch_after = 0.0
        # Serializes fetches so N concurrent requests on a cold or expired
        # cache open one connection to Keycloak, not N.
        self._lock = asyncio.Lock()

    def clear(self) -> None:
        self._keys = None
        self._fetched_at = 0.0
        self._retry_after = 0.0
        self._kid_refetch_after = 0.0

    def _is_fresh(self) -> bool:
        return (
            self._keys is not None
            and time.monotonic() - self._fetched_at < settings.jwks_cache_ttl_seconds
        )

    async def _fetch(self) -> PyJWKSet:
        """Pull the key set from the realm, or raise JWKSUnavailableError.

        Everything the endpoint can go wrong with ends up as one exception:
        a transport error, a non-2xx, a body that isn't JSON, and a body that
        is JSON but holds no key this service can verify with (an empty set,
        an error document, a bare list). Left uncaught, the last few surface
        as a 500 from a dependency whose whole contract is to answer 401.
        """
        try:
            async with httpx.AsyncClient(timeout=settings.jwks_timeout_seconds) as client:
                response = await client.get(settings.jwks_url)
            response.raise_for_status()
            keys = PyJWKSet.from_dict(response.json())
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, jwt.PyJWTError) as exc:
            msg = f"Could not load the key set at {settings.jwks_url}: {exc}"
            raise JWKSUnavailableError(msg) from exc
        self._keys = keys
        self._fetched_at = time.monotonic()
        return keys

    async def _load(self, *, force: bool = False) -> PyJWKSet:
        if not force and self._is_fresh():
            return self._keys  # type: ignore[return-value]
        fetched_at = self._fetched_at
        async with self._lock:
            # Whoever held the lock may have just fetched, in which case this
            # caller rides on their result -- including a forced load, where
            # freshness isn't the question but "did someone already refetch
            # while I waited" is.
            if self._is_fresh() if not force else self._fetched_at != fetched_at:
                return self._keys  # type: ignore[return-value]
            if time.monotonic() < self._retry_after:
                return self._stale_or_raise(JWKSUnavailableError("In the fetch-failure cooldown"))
            try:
                return await self._fetch()
            except JWKSUnavailableError as exc:
                self._retry_after = time.monotonic() + _FETCH_RETRY_COOLDOWN_SECONDS
                return self._stale_or_raise(exc)

    def _stale_or_raise(self, exc: JWKSUnavailableError) -> PyJWKSet:
        """Fall back on the last key set we did fetch, if there is one.

        Realm signing keys turn over on the order of months, so a key set
        that is past its TTL is still almost certainly the right one. Serving
        it through a Keycloak outage keeps partners working; refusing every
        request because a refresh failed would be an outage this service
        inflicted on itself.
        """
        if self._keys is None:
            raise exc
        log.warning("Serving the last known realm key set", error=str(exc))
        return self._keys

    @staticmethod
    def _select(keys: PyJWKSet, kid: str) -> PyJWK | None:
        try:
            return keys[kid]
        except KeyError:
            return None

    async def signing_key(self, kid: str) -> PyJWK | None:
        """The key with this id, refetching once if it isn't in the cache.

        Keycloak rotates realm keys without warning, and the first token
        signed by a new key arrives before the TTL expires. Refetching on an
        unknown id turns that into one extra request instead of a TTL's worth
        of refusals.
        """
        key = self._select(await self._load(), kid)
        if key is not None:
            return key

        # No await between reading the cooldown and setting it below, so a
        # burst of unknown kids can't all slip through the window. Keep it
        # that way: an await in between reopens the amplifier this guards.
        if time.monotonic() < self._kid_refetch_after:
            return None

        log.info("Refetching JWKS for an unknown key id", kid=kid)
        fetched_at = self._fetched_at
        key = self._select(await self._load(force=True), kid)
        if key is None and self._fetched_at != fetched_at:
            # Start the cooldown only once a freshly fetched key set really
            # didn't have the kid, which is what an advanced _fetched_at says.
            # A refetch that failed hands back the stale set instead of
            # raising, so without that test a Keycloak blip during a key
            # rotation would start the cooldown on evidence nobody gathered,
            # and keep refusing the new kid for a minute after Keycloak came
            # back. A failed fetch is already held off by _retry_after.
            self._kid_refetch_after = time.monotonic() + _KID_REFETCH_COOLDOWN_SECONDS
        return key


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
    token = request.headers.get("X-Access-Token", "").strip()
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
    except JWKSUnavailableError as exc:
        # The key set is unreachable or unusable. That is this service's
        # problem, not the caller's, but it must not open the door: refuse.
        log.warning("Could not load the realm JWKS", error=str(exc))
        raise _unauthorized() from exc
    if key is None:
        raise _unauthorized()

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=list(ALGORITHMS),
            audience=settings.audience,
            issuer=settings.issuer,
            leeway=settings.token_leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud", "typ"]},
        )
    except jwt.PyJWTError as exc:
        log.info("Refused a bearer token", reason=type(exc).__name__)
        raise _unauthorized() from exc
    if claims.get("typ") != ACCESS_TOKEN_TYPE:
        log.info("Refused a token that is not an access token", typ=claims.get("typ"))
        raise _unauthorized()
    return claims
