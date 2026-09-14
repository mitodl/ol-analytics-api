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

    default_page_size: int = 100
    max_page_size: int = 1000
    max_learner_ids: int = 100


settings = B2BLearnerRecordsSettings()
