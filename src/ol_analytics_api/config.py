"""Application settings, sourced entirely from environment variables.

No secret material or Vault role/mount names are hardcoded here — see
db/vault_credentials.py, which reads VAULT_ADDR / VAULT_ROLE / VAULT_K8S_MOUNT
at runtime to fetch short-lived StarRocks credentials from Vault's database
secrets engine (the same `database-starrocks-{env}/creds/app` path Dagster's
StarRocksResource and `bin/starrocks-auth --mode vault` use).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OL_ANALYTICS_API_")

    environment: str = "development"

    starrocks_host: str = "lakehouse-starrocks-fe-service.starrocks.svc.cluster.local"
    starrocks_port: int = 9030
    starrocks_database: str = "b2b_analytics"
    starrocks_pool_min_size: int = 1
    starrocks_pool_max_size: int = 10

    # Vault Kubernetes-auth wiring for dynamic StarRocks credentials. Unset in
    # local dev, where STARROCKS_USER/STARROCKS_PASSWORD (no OL_ANALYTICS_API_
    # prefix, matching bin/starrocks-auth's --output env convention) are read
    # directly instead — see db/vault_credentials.py.
    vault_addr: str = ""
    vault_role: str = ""
    vault_k8s_mount: str = ""
    vault_starrocks_mount: str = ""
    vault_starrocks_credential_role: str = "app"

    # Base URL of the MITx Online instance used for the org-manager round-trip
    # check (Phase 1 auth design — see hq#10594). No trailing slash.
    mitxonline_api_base_url: str = "https://mitxonline.mit.edu"
    mitxonline_manager_check_timeout_seconds: float = 5.0
    # How long an org-manager check result is cached per (sub, org_slug) pair,
    # to bound added latency on every analytics request.
    org_manager_cache_ttl_seconds: int = 60

    # Keycloak realm role required for MIT-admin-only endpoints (e.g.
    # contract-health). Checked against X-Userinfo's realm_access.roles.
    mit_admin_realm_role: str = "mit_contract_admin"

    # Minimum distinct-learner cohort size below which an aggregate row is
    # suppressed from API responses entirely (see Verification & QA epic).
    anonymization_floor: int = 5


settings = Settings()
