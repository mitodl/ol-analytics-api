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

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OL_ANALYTICS_API_")

    environment: str = "development"
    # Bare DEBUG/LOG_LEVEL (validation_alias bypasses env_prefix) — these are
    # cross-service conventions shared with mitxonline/mit-learn/learn-ai, not
    # this service's own namespaced config.
    debug: bool = Field(default=False, validation_alias="DEBUG")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    starrocks_host: str = "lakehouse-starrocks-fe-service.starrocks.svc.cluster.local"
    starrocks_port: int = 9030
    starrocks_pool_min_size: int = 1
    starrocks_pool_max_size: int = 10

    # DoS-surface bounds on the shared StarRocks pool. Without these a single
    # heavy/hung query holds a pooled connection indefinitely; enough of them
    # exhaust the fixed-size pool and every subsequent acquire — including the
    # readiness ping() — blocks forever, cascading the whole pod out of
    # rotation. See core/db/client.py.
    #
    # - acquire timeout: fail fast (surfaced as 503) instead of hanging when
    #   the pool is saturated, so a caller — and the readiness probe — never
    #   blocks on an exhausted pool.
    # - query timeout: StarRocks server-side per-statement timeout (session
    #   `query_timeout`, seconds), set on every connection. StarRocks aborts a
    #   query exceeding it and frees the connection, bounding how long any one
    #   query can hold a connection.
    # - connect timeout: bound establishing a brand-new connection so a
    #   slow/unreachable StarRocks can't wedge a pool fill.
    # - pool recycle: drop and rebuild connections older than this so a
    #   long-lived pool doesn't accumulate half-dead sockets (FE restarts,
    #   load-balancer idle-reaping).
    starrocks_pool_acquire_timeout_seconds: float = 5.0
    starrocks_query_timeout_seconds: int = 30
    starrocks_connect_timeout_seconds: float = 10.0
    starrocks_pool_recycle_seconds: int = 3600

    # Vault Kubernetes-auth wiring for dynamic StarRocks credentials. Unset in
    # local dev, where STARROCKS_USER/STARROCKS_PASSWORD (no OL_ANALYTICS_API_
    # prefix, matching bin/starrocks-auth's --output env convention) are read
    # directly instead — see db/vault_credentials.py.
    vault_addr: str = ""
    vault_role: str = ""
    vault_k8s_mount: str = ""
    vault_starrocks_mount: str = ""
    vault_starrocks_credential_role: str = "app"

    # Observability — bare env var names matching the OTel/Sentry convention
    # shared cluster-wide (see ol-infrastructure's application Pulumi.*.yaml
    # files for mitxonline/mit_learn/learn_ai). service_version defaults from
    # GIT_SHA, the same convention used in OTEL_RESOURCE_ATTRIBUTES elsewhere.
    service_name: str = Field(
        default="ol-analytics-api", validation_alias="OPENTELEMETRY_SERVICE_NAME"
    )
    service_version: str = Field(default="unknown", validation_alias="GIT_SHA")
    sentry_dsn: str = Field(default="", validation_alias="SENTRY_DSN")
    sentry_log_level: str = Field(default="ERROR", validation_alias="SENTRY_LOG_LEVEL")
    sentry_traces_sample_rate: float = Field(
        default=0.0, validation_alias="SENTRY_TRACES_SAMPLE_RATE"
    )
    sentry_profiles_sample_rate: float = Field(
        default=0.0, validation_alias="SENTRY_PROFILES_SAMPLE_RATE"
    )


settings = CoreSettings()
