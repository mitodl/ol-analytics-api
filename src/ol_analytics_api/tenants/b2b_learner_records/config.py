"""Policy settings owned by the b2b_learner_records tenant."""

from __future__ import annotations

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ol_analytics_api.core.db.identifiers import validate_sql_identifier


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
    issuer: str = "https://sso.ol.mit.edu/realms/olapps"

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
    def _derive_sso_urls(self) -> B2BLearnerRecordsSettings:
        base = self.issuer.rstrip("/")
        if not self.token_url:
            self.token_url = f"{base}/protocol/openid-connect/token"
        if not self.jwks_url:
            self.jwks_url = f"{base}/protocol/openid-connect/certs"
        return self


settings = B2BLearnerRecordsSettings()
