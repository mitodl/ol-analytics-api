"""What the learner-records tenant accepts as proof of identity.

The point of these tests is that the gateway is not in the trust path. A
request that reaches the pod directly, carrying whatever headers its sender
chose, gets exactly as far as its token's signature takes it.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import jwt
import pytest
import structlog.testing
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from ol_analytics_api.core.config import settings as core_settings
from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_learner_records import token as token_module
from ol_analytics_api.tenants.b2b_learner_records.config import (
    PRODUCTION_ISSUER,
    B2BLearnerRecordsSettings,
    settings,
)
from ol_analytics_api.tenants.b2b_learner_records.token import (
    ACCESS_TOKEN_TYPE,
    CONTRACT_END_DATE_CLAIM,
    CONTRACT_ENDED_DETAIL,
    INVALID_TOKEN_DETAIL,
    contract_access_through,
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
    assert response.json() == {"code": "unauthorized", "detail": INVALID_TOKEN_DETAIL}


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
    assert response.json() == {"code": "unauthorized", "detail": INVALID_TOKEN_DETAIL}, case


async def test_a_token_signed_by_another_key_is_refused(app, realm_keys):  # noqa: ARG001
    """Right kid, wrong key: what an attacker who read the JWKS would try."""
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = await _get(app, bearer(mint(PARTNER_CLAIMS, key=impostor)))
    assert response.status_code == 401


async def test_an_unsigned_token_is_refused(app, realm_keys):  # noqa: ARG001
    """alg=none, the oldest JWT hole. Only RS256 is accepted."""
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "typ": ACCESS_TOKEN_TYPE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        key=None,
        algorithm="none",
        headers={"kid": KID},
    )
    response = await _get(app, bearer(token))
    assert response.status_code == 401


async def test_a_token_with_no_kid_is_refused_without_touching_the_key_set(app, realm_keys):
    """Refused on the missing kid itself. Without that guard it would still
    be refused, but only after a pointless fetch, and one bad header would be
    enough to make a caller reach Keycloak."""
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "typ": ACCESS_TOKEN_TYPE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        signing_key(),
        algorithm="RS256",
    )
    response = await _get(app, bearer(token))
    assert response.status_code == 401
    assert realm_keys.get_requests(url=JWKS_URL) == []


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
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "typ": ACCESS_TOKEN_TYPE,
            "iat": int(time.time()),
            **PARTNER_CLAIMS,
        },
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


async def test_a_rotation_that_lands_during_an_outage_recovers(app, httpx_mock, monkeypatch):
    """Keycloak blips while a new key is being rotated in.

    The forced refetch fails and falls back on the stale key set, so the new
    kid still isn't there. That is not evidence the realm lacks the kid, so
    it must not start the unknown-kid cooldown: doing so would keep refusing
    the rotated key for a minute after Keycloak came back.
    """
    monkeypatch.setattr(token_module, "_FETCH_RETRY_COOLDOWN_SECONDS", 0.0)
    httpx_mock.add_response(url=JWKS_URL, json=jwks(KID))
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200

    httpx_mock.add_response(url=JWKS_URL, status_code=503)
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS, kid=OTHER_KID)))).status_code == 401

    httpx_mock.add_response(url=JWKS_URL, json=jwks(KID, OTHER_KID))
    recovered = await _get(app, bearer(mint(PARTNER_CLAIMS, kid=OTHER_KID)))
    assert recovered.status_code == 200


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
    assert response.json() == {"code": "unauthorized", "detail": INVALID_TOKEN_DETAIL}


async def test_a_cold_fetch_failure_is_held_off_before_retrying(app, httpx_mock):
    """With no key set to fall back on, refuse; but don't let every arriving
    request pay the timeout in turn for as long as Keycloak is down."""
    httpx_mock.add_response(url=JWKS_URL, status_code=503, is_reusable=True)
    for _ in range(4):
        assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 401
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 1


async def test_the_tenant_recovers_once_the_key_set_is_reachable(app, httpx_mock, monkeypatch):
    """The hold-off above delays a retry; it must not prevent one."""
    monkeypatch.setattr(token_module, "_FETCH_RETRY_COOLDOWN_SECONDS", 0.0)
    httpx_mock.add_response(url=JWKS_URL, status_code=503)
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 401

    httpx_mock.add_response(url=JWKS_URL, json=jwks())
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200


async def test_a_stale_key_set_carries_the_tenant_through_a_keycloak_outage(
    app, httpx_mock, monkeypatch
):
    """Realm keys turn over on the order of months, so a key set past its TTL
    is still the right one. Refusing every request because a refresh failed
    would be an outage this service inflicted on itself."""
    httpx_mock.add_response(url=JWKS_URL, json=jwks())
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200

    monkeypatch.setattr(settings, "jwks_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(token_module, "_FETCH_RETRY_COOLDOWN_SECONDS", 0.0)
    httpx_mock.add_response(url=JWKS_URL, status_code=503, is_reusable=True)
    assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200
    assert len(httpx_mock.get_requests(url=JWKS_URL)) > 1


async def test_the_key_set_is_refetched_after_the_ttl(app, httpx_mock, monkeypatch):
    monkeypatch.setattr(settings, "jwks_cache_ttl_seconds", 0.0)
    httpx_mock.add_response(url=JWKS_URL, json=jwks(), is_reusable=True)
    for _ in range(2):
        assert (await _get(app, bearer(mint(PARTNER_CLAIMS)))).status_code == 200
    assert len(httpx_mock.get_requests(url=JWKS_URL)) == 2


async def test_concurrent_cold_requests_fetch_the_key_set_once(app, httpx_mock):
    """Pins the lock, not the cache.

    A mocked transport returns without ever suspending, so eight gathered
    requests would serialise themselves and pass this even with no lock at
    all. The sleep makes the fetch yield the way a real one does, so the
    other seven arrive while the first is still in flight.
    """
    fetches = 0

    async def slow_jwks(request):  # noqa: ARG001
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=jwks())

    httpx_mock.add_callback(slow_jwks, url=JWKS_URL, is_reusable=True)
    jwks_cache.clear()
    responses = await asyncio.gather(*(_get(app, bearer(mint(PARTNER_CLAIMS))) for _ in range(8)))
    assert [r.status_code for r in responses] == [200] * 8
    assert fetches == 1


async def test_an_id_token_for_the_same_client_is_refused(app, realm_keys):  # noqa: ARG001
    """An ID token minted for ol-analytics-api-client carries the realm's
    signature, this issuer and this audience. Only its token class tells it
    apart from an access token."""
    response = await _get(app, bearer(mint({**PARTNER_CLAIMS, "typ": "ID"})))
    assert response.status_code == 401
    assert response.json() == {"code": "unauthorized", "detail": INVALID_TOKEN_DETAIL}


async def test_a_token_with_no_token_class_is_refused(app, realm_keys):  # noqa: ARG001
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            **PARTNER_CLAIMS,
        },
        signing_key(),
        algorithm="RS256",
        headers={"kid": KID},
    )
    assert (await _get(app, bearer(token))).status_code == 401


async def test_a_symmetric_token_keyed_with_the_public_key_is_refused(app, realm_keys):  # noqa: ARG001
    """The other half of algorithm confusion: not an unsigned token but one
    signed with HS256, using the public key everyone can read out of the JWKS
    as the shared secret.

    Assembled by hand rather than with jwt.encode, which refuses to key HMAC
    with an asymmetric key. An attacker has no such scruples, so the test
    can't have them either.
    """
    public_pem = (
        signing_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    def segment(payload: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")

    signing_input = b".".join(
        (
            segment({"alg": "HS256", "typ": "JWT", "kid": KID}),
            segment(
                {
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "typ": ACCESS_TOKEN_TYPE,
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 300,
                    **PARTNER_CLAIMS,
                }
            ),
        )
    )
    signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    token = b".".join((signing_input, base64.urlsafe_b64encode(signature).rstrip(b"=")))
    assert (await _get(app, bearer(token.decode()))).status_code == 401


async def test_the_audience_may_be_a_list(app, realm_keys):  # noqa: ARG001
    """The shape Keycloak actually emits once a token carries more than one
    audience. The single-string form the other tests use is the simpler case,
    not the real one."""
    token = mint(PARTNER_CLAIMS, audience=[AUDIENCE, "account"])
    assert (await _get(app, bearer(token))).status_code == 200


@pytest.mark.parametrize(
    ("body", "case"),
    [
        ({"keys": []}, "a key set with no keys"),
        ({"error": "realm not found"}, "an error document served as 200"),
        (["not", "a", "key", "set"], "a bare list"),
    ],
)
async def test_an_unusable_key_set_refuses_rather_than_erroring(app, httpx_mock, body, case):
    """A proxy or a mid-import realm can answer 200 with a body that parses
    as JSON but holds no usable key. This dependency's contract is to answer
    401; letting the parse error escape would make it a 500 instead."""
    httpx_mock.add_response(url=JWKS_URL, json=body, is_reusable=True)
    response = await _get(app, bearer(mint(PARTNER_CLAIMS)))
    assert response.status_code == 401, case
    assert response.json() == {"code": "unauthorized", "detail": INVALID_TOKEN_DETAIL}, case


def test_settings_derive_the_sso_urls_from_the_issuer():
    """One setting moves a deployment to another realm; four can drift."""
    assert settings.__class__(issuer="https://sso.example/realms/r").jwks_url == (
        "https://sso.example/realms/r/protocol/openid-connect/certs"
    )
    assert settings.__class__(issuer="https://sso.example/realms/r/").token_url == (
        "https://sso.example/realms/r/protocol/openid-connect/token"
    )


@pytest.mark.parametrize("environment", ["qa", "ci", "rc"])
def test_a_deployed_environment_must_name_its_own_realm(monkeypatch, environment):
    """Leaving the issuer unset outside production would have the pod verify
    against a realm that never issued the token, and the 401 that follows
    reads like a bad credential."""
    monkeypatch.setattr(core_settings, "environment", environment)
    with pytest.raises(ValidationError, match="ISSUER is unset"):
        B2BLearnerRecordsSettings(issuer=PRODUCTION_ISSUER)


@pytest.mark.parametrize("environment", ["production", "development"])
def test_the_default_realm_is_accepted_where_it_is_the_right_one(monkeypatch, environment):
    monkeypatch.setattr(core_settings, "environment", environment)
    assert B2BLearnerRecordsSettings(issuer=PRODUCTION_ISSUER).issuer == PRODUCTION_ISSUER


def _with_contract_end(value: object) -> dict:
    return {**PARTNER_CLAIMS, CONTRACT_END_DATE_CLAIM: value}


async def test_a_token_past_its_contract_end_is_refused(app, realm_keys):  # noqa: ARG001
    ended = (datetime.now(UTC).date() - timedelta(days=2)).isoformat()
    response = await _get(app, bearer(mint(_with_contract_end(ended))))
    assert response.status_code == 401
    assert response.json() == {
        "code": "unauthorized",
        "detail": f"{CONTRACT_ENDED_DETAIL} on {ended}",
    }


async def test_a_token_before_its_contract_end_is_accepted(app, realm_keys):  # noqa: ARG001
    ends = (datetime.now(UTC).date() + timedelta(days=2)).isoformat()
    response = await _get(app, bearer(mint(_with_contract_end(ends))))
    assert response.status_code == 200


async def test_a_token_with_no_contract_end_is_accepted(app, realm_keys):  # noqa: ARG001
    """Clients provisioned without an end date are open-ended by design.
    Reading absence as expired would revoke all of them on deploy."""
    response = await _get(app, bearer(mint(PARTNER_CLAIMS)))
    assert response.status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        "2027-6-30",
        "20270630",
        "2027-W26-3",
        "2027-06-30T00:00:00",
        "",
        "never",
        None,
        20270630,
        ["2027-06-30"],
    ],
)
async def test_a_malformed_contract_end_is_refused(app, realm_keys, value):  # noqa: ARG001
    """Anything other than the mapper's YYYY-MM-DD refuses, because reading it
    as "no end date" is the direction that fails open."""
    response = await _get(app, bearer(mint(_with_contract_end(value))))
    assert response.status_code == 401
    assert response.json()["detail"] == INVALID_TOKEN_DETAIL


@pytest.mark.parametrize(
    ("now", "accepted"),
    [
        (datetime(2027, 6, 30, 23, 59, 59, tzinfo=UTC), True),
        (datetime(2027, 7, 1, 11, 59, 59, tzinfo=UTC), True),
        (datetime(2027, 7, 1, 12, 0, 0, tzinfo=UTC), False),
    ],
)
async def test_access_runs_to_the_end_of_the_date_anywhere_on_earth(
    app,
    realm_keys,  # noqa: ARG001
    monkeypatch,
    now,
    accepted,
):
    monkeypatch.setattr(token_module, "_now", lambda: now)
    response = await _get(app, bearer(mint(_with_contract_end("2027-06-30"))))
    assert response.status_code == (200 if accepted else 401)


def test_the_last_day_representable_is_an_end_date_not_an_error():
    through = contract_access_through({CONTRACT_END_DATE_CLAIM: date.max.isoformat()})
    assert through is not None
    assert datetime.now(UTC) < through


async def test_the_access_log_carries_the_contract_end_date(app, realm_keys):  # noqa: ARG001
    ends = (datetime.now(UTC).date() + timedelta(days=2)).isoformat()
    with structlog.testing.capture_logs() as logs:
        response = await _get(app, bearer(mint(_with_contract_end(ends))))
    assert response.status_code == 200
    access = [entry for entry in logs if entry["event"] == "learner_records_access"]
    assert [entry["contract_end_date"] for entry in access] == [ends]
