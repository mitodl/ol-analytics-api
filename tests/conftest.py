"""Shared fixtures.

Mostly the signing key the b2b_learner_records tenant verifies bearer tokens
against. That tenant no longer trusts X-Userinfo, so its tests have to present
a real RS256 token from a realm whose JWKS the service can fetch.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ol_analytics_api.tenants.b2b_learner_records.config import settings
from ol_analytics_api.tenants.b2b_learner_records.token import ACCESS_TOKEN_TYPE, jwks_cache

ISSUER = "https://sso.test.example/realms/olapps"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
AUDIENCE = "ol-analytics-api-client"
KID = "realm-key-1"
OTHER_KID = "realm-key-2"

# One 2048-bit key generation per test session. RSA keygen is slow enough that
# doing it per test is noticeable, and nothing here depends on key freshness.
_KEYS = {KID: rsa.generate_private_key(public_exponent=65537, key_size=2048)}


def signing_key(kid: str = KID) -> rsa.RSAPrivateKey:
    if kid not in _KEYS:
        _KEYS[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _KEYS[kid]


def jwks(*kids: str) -> dict:
    """The realm's published key set, as Keycloak's certs endpoint returns it."""
    return {
        "keys": [
            {
                **jwt.algorithms.RSAAlgorithm.to_jwk(signing_key(kid).public_key(), as_dict=True),
                "kid": kid,
                "alg": "RS256",
                "use": "sig",
            }
            for kid in (kids or (KID,))
        ]
    }


def mint(
    claims: dict | None = None,
    *,
    kid: str = KID,
    issuer: str = ISSUER,
    audience: str | list[str] = AUDIENCE,
    lifetime_seconds: int = 300,
    issued_at: float | None = None,
    algorithm: str = "RS256",
    key: object | None = None,
) -> str:
    """A Keycloak-shaped access token signed by the test realm."""
    now = time.time() if issued_at is None else issued_at
    payload = {
        "iss": issuer,
        "aud": audience,
        # Keycloak's token class. An ID token carries "ID" here, which is the
        # difference the tenant checks, so it belongs in the default shape.
        "typ": ACCESS_TOKEN_TYPE,
        "iat": int(now),
        "exp": int(now + lifetime_seconds),
        **(claims or {}),
    }
    return jwt.encode(
        payload,
        key if key is not None else signing_key(kid),  # type: ignore[arg-type]
        algorithm=algorithm,
        headers={"kid": kid},
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _test_realm(monkeypatch):
    """Point the tenant at the test realm and hand back a clean key cache.

    The cache is a process-wide singleton, so a key set left over from one
    test would answer another test's fetch and make it pass for the wrong
    reason.
    """
    monkeypatch.setattr(settings, "issuer", ISSUER)
    monkeypatch.setattr(settings, "jwks_url", JWKS_URL)
    monkeypatch.setattr(settings, "audience", AUDIENCE)
    jwks_cache.clear()
    yield
    jwks_cache.clear()


@pytest.fixture
def realm_keys(httpx_mock):
    """Serve the realm's JWKS for as many fetches as a test makes.

    Optional because a test that is refused before verification (no bearer
    token at all) never fetches, and reusable because the cache is cleared
    between tests.
    """
    httpx_mock.add_response(url=JWKS_URL, json=jwks(), is_reusable=True, is_optional=True)
    return httpx_mock
