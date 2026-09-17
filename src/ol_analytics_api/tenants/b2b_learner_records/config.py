"""Policy settings owned by the b2b_learner_records tenant."""

from __future__ import annotations

from pydantic import field_validator
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

    # Advertised in this tenant's OpenAPI security scheme so generated clients
    # know where to get a token. APISIX, not this service, validates tokens.
    token_url: str = "https://sso.ol.mit.edu/realms/olapps/protocol/openid-connect/token"  # noqa: S105

    default_page_size: int = 100
    max_page_size: int = 1000
    max_learner_ids: int = 100

    # Whether a learner with no recorded consent decision shares their outcomes.
    # No consent field exists upstream yet, so today this decides every record.
    # False fails closed, and is the default so a deployment that never sets it
    # discloses nothing; deployments opt in through ol-infrastructure.
    consent_fail_open: bool = False


settings = B2BLearnerRecordsSettings()
