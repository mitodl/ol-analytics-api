"""Settings shared by every tenant: StarRocks connectivity and Vault wiring.

Tenant-specific policy (which schema to query, which auth checks to run,
suppression thresholds, upstream services to call) lives in each tenant's
own `config.py` under `tenants/<name>/`, not here — this file only covers
infrastructure every tenant shares regardless of audience or governance
model.

No secret material or Vault role/mount names are hardcoded — see
db/vault_credentials.py, which reads these at runtime to fetch short-lived
StarRocks credentials from Vault's database secrets engine (the same
`database-starrocks-{env}/creds/app` path Dagster's StarRocksResource and
`bin/starrocks-auth --mode vault` use).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OL_ANALYTICS_API_")

    environment: str = "development"

    starrocks_host: str = "lakehouse-starrocks-fe-service.starrocks.svc.cluster.local"
    starrocks_port: int = 9030
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


settings = CoreSettings()
