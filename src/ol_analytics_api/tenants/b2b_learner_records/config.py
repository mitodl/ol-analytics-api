"""Policy settings owned by the b2b_learner_records tenant."""

from __future__ import annotations

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ol_analytics_api.core.config import settings as core_settings
from ol_analytics_api.core.db.identifiers import validate_sql_identifier

PRODUCTION_ISSUER = "https://sso.ol.mit.edu/realms/olapps"

# Where leaving the issuer at its production default is not evidence of a
# misconfiguration: production itself, and a developer's machine, which has
# no partner tokens to verify either way.
_ISSUER_DEFAULT_IS_FINE = ("development", "production")


class B2BLearnerRecordsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OL_ANALYTICS_API_B2B_LEARNER_RECORDS_")

    # A separate StarRocks database from b2b_analytics, so these PII-bearing
    # views can be granted to this tenant alone once tenants get their own
    # StarRocks roles. Spliced into SQL, so validated as an identifier.
    starrocks_schema: str = "b2b_learner_records"

    @field_validator("starrocks_schema")
    @classmethod
    def _validate_starrocks_schema(cls, value: str) -> str:
        return validate_sql_identifier(value)

    # The Keycloak realm that issues partner client-credentials tokens. Every
    # other SSO URL below is derived from it, so pointing a deployment at a
    # different realm is one setting, not four that can drift apart.
    # ol-infrastructure templates it out of the same Vault entry the gateway
    # route reads. If that ever fails to render, the default below would have
    # a QA pod verifying against the production realm while APISIX in front of
    # it used QA's -- every partner token refused, for a reason nothing in the
    # refusal names. _reject_the_wrong_realm turns that into a failed start.
    issuer: str = PRODUCTION_ISSUER

    # Tokens must name this service in `aud`. The learner-records client scope
    # adds it through an audience mapper (ol-infrastructure
    # substructure/keycloak/learner_records.py); a token minted for another
    # olapps client is signed by the same realm key and is refused on this
    # check alone.
    audience: str = "ol-analytics-api-client"

    # Advertised in this tenant's OpenAPI security scheme so generated clients
    # know where to get a token.
    token_url: str = ""

    # Verification keys. Cached for the TTL and refetched early on a kid this
    # service has not seen, so a realm key rotation costs one fetch rather
    # than a TTL of 401s.
    jwks_url: str = ""
    jwks_cache_ttl_seconds: float = 3600.0
    jwks_timeout_seconds: float = 5.0

    # Tolerance for clock skew between Keycloak and this pod when checking
    # exp/nbf. The partner access token lifespan is 300s, so this stays well
    # under it.
    token_leeway_seconds: float = 30.0

    default_page_size: int = 100
    max_page_size: int = 1000
    max_learner_ids: int = 100

    # Whether a learner with no recorded consent decision shares their outcomes.
    # No consent field exists upstream yet, so today this decides every record.
    # False fails closed, and is the default so a deployment that never sets it
    # discloses nothing; deployments opt in through ol-infrastructure.
    consent_fail_open: bool = False

    @model_validator(mode="after")
    def _reject_the_wrong_realm(self) -> B2BLearnerRecordsSettings:
        """Refuse to start rather than verify against another environment.

        Every failure mode of this setting is silent: a pod that verifies
        partner tokens against a realm that never issued them refuses every
        one of them, and the 401 it returns looks like a bad credential.
        """
        if self.issuer == PRODUCTION_ISSUER and core_settings.environment not in (
            _ISSUER_DEFAULT_IS_FINE
        ):
            msg = (
                f"{self.model_config['env_prefix']}ISSUER is unset in the "
                f"{core_settings.environment!r} environment, so partner tokens would be "
                f"verified against the production realm ({PRODUCTION_ISSUER}). Set it to "
                "this environment's Keycloak realm URL."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _derive_sso_urls(self) -> B2BLearnerRecordsSettings:
        base = self.issuer.rstrip("/")
        if not self.token_url:
            self.token_url = f"{base}/protocol/openid-connect/token"
        if not self.jwks_url:
            self.jwks_url = f"{base}/protocol/openid-connect/certs"
        return self


settings = B2BLearnerRecordsSettings()
