"""What the learner-records tenant accepts as proof of identity.

The point of these tests is that the gateway is not in the trust path. A
request that reaches the pod directly, carrying whatever headers its sender
chose, gets exactly as far as its token's signature takes it.
"""

import asyncio
import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_learner_records.config import settings
from ol_analytics_api.tenants.b2b_learner_records.token import (
    INVALID_TOKEN_DETAIL,
    jwks_cache,
)
from tests.conftest import (
    AUDIENCE,
    ISSUER,
    JWKS_URL,
    KID,
    OTHER_KID,
    bearer,
    jwks,
    mint,
    signing_key,
)

BASE = "/api/v1/learner-records"
ORG_ID = "8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21"
PATH = f"{BASE}/organizations/{ORG_ID}/learners"

PARTNER_CLAIMS = {
    "azp": "contoso-lms",
    "scope": "basic learner-records:read",
    "learner_records_organizations": [ORG_ID],
}


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _stub_pool(monkeypatch):
    async def fetch_all(query, params=()):  # noqa: ARG001
        if "COUNT(*)" in query:
            return [{"total_count": 0, "outcomes_withheld_count": 0}]
        return []

    monkeypatch.setattr("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", fetch_all)


async def _get(app, headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(PATH, headers=headers)


def _userinfo(claims: dict) -> dict[str, str]:
    return {"X-Userinfo": base64.b64encode(json.dumps(claims).encode()).decode()}


async def test_a_valid_token_is_accepted(app, realm_keys):  # noqa: ARG001
    response = await _get(app, bearer(mint(PARTNER_CLAIMS)))
    assert response.status_code == 200


async def test_a_forged_x_userinfo_alone_is_refused(app, realm_keys):  # noqa: ARG001
    """The attack this tenant exists to close: a pod-to-pod request that
    skips APISIX and asserts its own claims."""
    response = await _get(app, _userinfo(PARTNER_CLAIMS))
    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_TOKEN_DETAIL}


async def test_a_forged_x_userinfo_cannot_widen_a_valid_token(app, realm_keys):  # noqa: ARG001
    """A token granting nothing plus an X-Userinfo granting everything is
    still a token granting nothing: the header is never read."""
    narrow = mint({**PARTNER_CLAIMS, "learner_records_organizations": []})
    wide = _userinfo(PARTNER_CLAIMS)
    response = await _get(app, {**bearer(narrow), **wide})
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("headers", "case"),
    [
        ({}, "no headers at all"),
        ({"Authorization": "Bearer "}, "an empty bearer"),
        ({"Authorization": "Basic Zm9vOmJhcg=="}, "the wrong scheme"),
        ({"Authorization": "Bearer not-a-jwt"}, "a token that isn't a JWT"),
    ],
)
async def test_malformed_credentials_are_refused(app, realm_keys, headers, case):  # noqa: ARG001
    response = await _get(app, headers)
    assert response.status_code == 401, case
    assert response.json() == {"detail": INVALID_TOKEN_DETAIL}, case


async def test_a_token_signed_by_another_key_is_refused(app, realm_keys):  # noqa: ARG001
    """Right kid, wrong key: what an attacker who read the JWKS would try."""
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = await _get(app, bearer(mint(PARTNER_CLAIMS, key=impostor)))
    assert response.status_code == 401


async def test_an_unsigned_token_is_refused(app, realm_keys):  # noqa: ARG001
    """alg=none, the oldest JWT hole. Only RS256 is accepted."""
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "iat": int(time.time()), "exp": int(time.time()) + 300},
        key=None,
        algorithm="none",
        headers={"kid": KID},
    )
    response = await _get(app, bearer(token))
    assert response.status_code == 401


async def test_a_token_with_no_kid_is_refused(app, realm_keys):  # noqa: ARG001
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "iat": int(time.time()), "exp": int(time.time()) + 300},
        signing_key(),
        algorithm="RS256",
    )
    response = await _get(app, bearer(token))
    assert response.status_code == 401


async def test_a_token_for_another_audience_is_refused(app, realm_keys):  # noqa: ARG001
    """Every olapps client's token is signed by the same realm key, so the
    audience is what separates this service's callers from everyone else's."""
    response = await _get(app, bearer(mint(PARTNER_CLAIMS, audience="mitxonline-client")))
    assert response.status_code == 401


async def test_a_token_from_another_issuer_is_refused(app, realm_keys):  # noqa: ARG001
    response = await _get(
        app, bearer(mint(PARTNER_CLAIMS, issuer="https://sso.test.example/realms/other"))
    )
    assert response.status_code == 401


async def test_an_expired_token_is_refused(app, realm_keys):  # noqa: ARG001
    stale = mint(PARTNER_CLAIMS, issued_at=time.time() - 3600, lifetime_seconds=300)
    response = await _get(app, bearer(stale))
    assert response.status_code == 401


async def test_a_token_that_is_not_valid_yet_is_refused(app, realm_keys):  # noqa: ARG001
    future = mint({**PARTNER_CLAIMS, "nbf": int(time.time() + 3600)})
    response = await _get(app, bearer(future))
    assert response.status_code == 401


async def test_a_token_missing_exp_is_refused(app, realm_keys):  # noqa: ARG001
    """A token with no expiry can never be aged out, which is the only
    revocation this design has."""
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "iat": int(time.time()), **PARTNER_CLAIMS},
        signing_key(),
        algorithm="RS256",
        headers={"kid": KID},
    )
    response = await _get(app, bearer(token))
    assert response.status_code == 401


async def test_the_gateway_access_token_header_is_accepted_and_verified(app, realm_keys):  # noqa: ARG001
    """APISIX puts the token it validated in X-Access-Token. Reading it is
    safe because it is verified like any other."""
    accepted = await _get(app, {"X-Access-Token": mint(PARTNER_CLAIMS)})
    assert accepted.status_code == 200

    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = await _get(app, {"X-Access-Token": mint(PARTNER_CLAIMS, key=impostor)})
    assert forged.status_code == 401


async def test_the_key_set_is_fetched_once_and_reused(app, realm_keys):
    for _ in range(3):
        assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200
    assert len(realm_keys.get_requests(url=JWKS_URL)) == 1


async def test_an_unknown_key_id_refetches_the_key_set(app, httpx_mock):
    """A realm key rotation costs one extra fetch, not a cache TTL of 401s."""
    httpx_mock.add_response(url=JWKS_URL, json=jwks(KID))
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200

    httpx_mock.add_response(url=JWKS_URL, json=jwks(KID, OTHER_KID))
    rotated = await _get(app, bearer(mint(PARTNER_CLAIMS, kid=OTHER_KID)))
    assert rotated.status_code == 200
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 2


async def test_a_junk_key_id_does_not_refetch_on_every_request(app, httpx_mock):
    """Otherwise a stream of forged tokens is a request amplifier pointed at
    Keycloak."""
    httpx_mock.add_response(url=JWKS_URL, json=jwks(), is_reusable=True)
    for _ in range(5):
        response = await _get(app, bearer(mint(PARTNER_CLAIMS, kid="no-such-key")))
        assert response.status_code == 401
    # One cold fetch, then one refetch for the first unknown kid; the cooldown
    # absorbs the rest.
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 2


async def test_an_unreachable_key_set_refuses_rather_than_opening_up(app, httpx_mock):
    httpx_mock.add_response(url=JWKS_URL, status_code=503, is_reusable=True)
    response = await _get(app, bearer(mint(PARTNER_CLAIMS)))
    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_TOKEN_DETAIL}


async def test_a_failed_fetch_is_not_cached(app, httpx_mock):
    httpx_mock.add_response(url=JWKS_URL, status_code=503)
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 401

    httpx_mock.add_response(url=JWKS_URL, json=jwks())
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200


async def test_the_key_set_is_refetched_after_the_ttl(app, httpx_mock, monkeypatch):
    monkeypatch.setattr(settings, "jwks_cache_ttl_seconds", 0.0)
    httpx_mock.add_response(url=JWKS_URL, json=jwks(), is_reusable=True)
    for _ in range(2):
        assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 2


async def test_concurrent_cold_requests_fetch_the_key_set_once(app, httpx_mock):
    httpx_mock.add_response(url=JWKS_URL, json=jwks(), is_reusable=True)
    jwks_cache.clear()
    responses = await asyncio.gather(*(_get(app, bearer(mint(PARTNER_CLAIMS))) for _ in range(8)))
    assert [r.status_code for r in responses] == [200] * 8
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 1


def test_settings_derive_the_sso_urls_from_the_issuer():
    """One setting moves a deployment to another realm; four can drift."""
    assert settings.__class__(issuer="https://sso.example/realms/r").jwks_url == (
        "https://sso.example/realms/r/protocol/openid-connect/certs"
    )
    assert settings.__class__(issuer="https://sso.example/realms/r/").token_url == (
        "https://sso.example/realms/r/protocol/openid-connect/token"
    )
