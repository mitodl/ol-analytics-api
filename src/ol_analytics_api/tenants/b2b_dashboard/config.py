"""Policy settings owned by the b2b_dashboard tenant.

Everything here is specific to this audience (MIT Learn's org-manager
dashboard) — a different tenant serving a different consumer can define an
entirely different schema, auth model, or suppression policy in its own
`config.py` without touching this one.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ol_analytics_api.core.db.identifiers import validate_sql_identifier


class B2BDashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OL_ANALYTICS_API_B2B_DASHBOARD_")

    # StarRocks schema this tenant's queries are scoped to (default_catalog).
    # Validated as a safe SQL identifier since it's spliced into query
    # strings directly — StarRocks can't parameterize identifiers.
    starrocks_schema: str = "b2b_analytics"

    @field_validator("starrocks_schema")
    @classmethod
    def _validate_starrocks_schema(cls, value: str) -> str:
        return validate_sql_identifier(value)

    # Base URL of the MITx Online instance used for the org-manager round-trip
    # check (Phase 1 auth design — see hq#10594). No trailing slash.
    mitxonline_api_base_url: str = "https://mitxonline.mit.edu"
    mitxonline_manager_check_timeout_seconds: float = 5.0

    # OAuth2 client-credentials used to authenticate the org-manager check to
    # MITx Online. Empty defaults keep local dev and the test suite runnable
    # without credentials; a deployed environment must set both (they come
    # from Vault via the Pulumi stack). There is no sensible default for a
    # secret, and a wrong one fails loudly at the token endpoint rather than
    # silently granting anything.
    mitxonline_client_id: str = ""
    mitxonline_client_secret: str = ""
    # Scope registered on the MITx Online side for the service endpoint
    # (mitodl/mitxonline#3807). Only ever granted to a service Application.
    mitxonline_oauth_scope: str = "b2b:manager-check"
    # How long an org-manager check result is cached per (sub, org_slug) pair,
    # to bound added latency on every analytics request.
    org_manager_cache_ttl_seconds: int = 60

    # Keycloak realm role required for MIT-admin-only endpoints (e.g.
    # contract-health). Checked against X-Userinfo's realm_access.roles.
    mit_admin_realm_role: str = "mit_contract_admin"

    # Minimum distinct-learner cohort size below which an aggregate row is
    # suppressed from API responses entirely (see Verification & QA epic).
    anonymization_floor: int = 5

    # Pagination bounds on every list endpoint. The org-scoped grains
    # (content-engagement, enrollment-funnel) are otherwise unbounded — a
    # large org would load its whole result set into a memory-limited pod,
    # suppress in Python, and serialize it. Every endpoint LIMITs to
    # default_page_size (client-overridable up to max_page_size) so no single
    # request can pull an unbounded result (see the DoS-surface task).
    default_page_size: int = 100
    max_page_size: int = 1000


settings = B2BDashboardSettings()
