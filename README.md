# ol-analytics-api

Multi-tenant, read-only FastAPI analytics gateway over StarRocks. Each
consumer of this service is a **tenant**: an independent FastAPI sub-app
with its own routes, auth/governance model, and OpenAPI docs, mounted onto
one root app. Tenants share only the underlying StarRocks connection pool —
nothing else.

The first tenant is `b2b_dashboard`: aggregated (no individual learner PII)
B2B site-license analytics — contract utilization, enrollment/completion
funnel, monthly engagement trend, program funnel, content engagement depth,
MIT-admin contract health — for the MIT Learn dashboard at
`/dashboard/organization/[orgSlug]/analytics`.

## Architecture

```
dbt (organization_administration_report, Iceberg)
  -> StarRocks materialized views (ol-data-platform, models/b2b_analytics/*.sql)
  -> this service:
       main.py (shared StarRocks pool, mounts tenant sub-apps)
         -> tenants/b2b_dashboard  (mounted at /api/v1/analytics)
         -> tenants/<next-tenant>  (mounted at its own prefix)
  -> each tenant's own consumer (MIT Learn dashboard, future partner/internal tools, ...)
```

```
src/ol_analytics_api/
  main.py                    # root app: shared lifespan (StarRocks pool via Vault),
                              # observability init, /health/*, TENANTS registry, mounts each sub-app
  core/                       # shared by every tenant, no tenant-specific policy
    config.py                 # StarRocks host/port, Vault K8s-auth wiring, observability settings
    health.py                  # tiered K8s health checks — /health/{startup,readiness,liveness}/
    db/client.py               # aiomysql connection pool (one pool, all tenants)
    db/vault_credentials.py    # dynamic StarRocks creds via Vault K8s auth
    db/identifiers.py          # SQL-identifier validation for schema names spliced into queries
    auth/userinfo.py           # generic X-Userinfo decode (APISIX forwards this to every tenant)
    anonymization.py           # generic k-anonymity-style row suppression, floor is a tenant param
    observability/
      processors.py             # structlog trace_id/span_id + k8s pod/namespace injection
      logging.py                 # structlog config: JSON in prod, console in dev
      telemetry.py                # OpenTelemetry SDK + auto-instrumentation (traces)
      sentry.py                   # Sentry init
      middleware.py                # structured per-request access log, shared by every app instance
  tenants/
    b2b_dashboard/
      app.py                   # FastAPI() sub-app instance, includes this tenant's routers
      config.py                # this tenant's policy: schema, MITx Online URL, admin role, floor
      auth.py                  # this tenant's governance gates (require_org_manager, require_mit_admin)
      mitxonline_client.py     # org-manager round-trip to MITx Online
      models.py                # SQLModel response schemas for this tenant's 6 MVs
      routers/
        organizations.py       # relative paths — mount point supplies the /api/v1/analytics prefix
        admin.py
```

### Adding a new tenant

A new consumer — a different internal tool, a partner integration, a public
read-only feed — gets its own package under `tenants/`, following the same
shape as `b2b_dashboard/`: an `app.py` exposing a `FastAPI()` instance, its
own `config.py`/`auth.py`/`routers/`. It can define completely different
auth (API keys, no auth, a different Keycloak realm role), a different
StarRocks schema, and its own suppression policy — none of that is shared
state. Wire it up with one entry in `main.py`'s `TENANTS` list — a `Tenant`
also carries optional `on_startup`/`on_shutdown` hooks, since a mounted
sub-app's own `lifespan=` is never invoked by the ASGI server (only the
root app's is), so tenant-owned resources (e.g. an httpx client) start up
and shut down via hooks the root lifespan calls explicitly:

```python
TENANTS: list[Tenant] = [
    Tenant(
        "/api/v1/analytics",
        b2b_dashboard.app,
        on_startup=b2b_dashboard.on_startup,
        on_shutdown=b2b_dashboard.on_shutdown,
    ),
    Tenant("/api/v1/<new-tenant>", new_tenant.app),
]
```

Each tenant gets independent OpenAPI docs at `<mount-path>/docs`.

### Auth

APISIX validates the Keycloak JWT in front of this service and forwards
decoded claims as a base64-encoded JSON blob in the `X-Userinfo` header
(`core/auth/userinfo.py`, shared by every tenant) — this service does not
validate tokens or fetch JWKS itself. What happens with those claims is
entirely up to each tenant: `b2b_dashboard`'s org-manager check round-trips
to MITx Online (`tenants/b2b_dashboard/mitxonline_client.py`) and its
MIT-admin check uses a Keycloak realm role
(`tenants/b2b_dashboard/auth.py`) — see hq#10594 for the full design. A
different tenant is free to use a different governance model entirely.

### Observability

Everything here mirrors the conventions `mitol-django-observability` gives
Django services (mitxonline, mit-learn, learn-ai), reimplemented without a
Django dependency so a FastAPI-native service can share the same log shape,
trace pipeline, and K8s probe contract:

- **Structured logging** — `structlog`, JSON in production / colorized
  console when `DEBUG=true`. Every log line carries `trace_id`/`span_id`
  (when a span is active) and `pod_name`/`namespace`/`node_name` (when the
  matching `KUBERNETES_*` env vars are set), via processors ported verbatim
  from `mitol-django-observability` — same field names as every other
  service's logs, so Loki/Grafana queries work identically here.
- **Access logs** — one structured JSON line per request (method, path,
  status, duration), via `core/observability/middleware.py`, added to the
  root app only — Starlette's `Mount` runs a tenant sub-app inside the root
  app's request lifecycle, so the root app's middleware already sees a
  tenant's final response; adding it to tenant sub-apps too would log every
  tenant request twice. uvicorn's own access log is disabled
  (`--no-access-log`) to avoid duplicating this in a different, unstructured
  format.
- **Tracing** — OpenTelemetry, activated when `OTEL_EXPORTER_OTLP_ENDPOINT`
  or `OPENTELEMETRY_ENDPOINT` is set (or `DEBUG=true`) — no separate
  "enabled" flag, matching learn-ai's current convention. Exports via OTLP
  HTTP to Grafana Alloy
  (`http://grafana-k8s-monitoring-alloy-receiver.grafana.svc.cluster.local:4318`
  in this cluster). FastAPI and httpx are auto-instrumented via OTel's
  standard entry-point discovery — installing
  `opentelemetry-instrumentation-<x>` is enough, no code change needed. No
  separate OTel Logs pipeline: trace/log correlation happens via the
  `trace_id`/`span_id` fields structlog injects into stdout JSON, which
  Alloy scrapes as logs — same as the Django services.
- **Errors** — Sentry, via `core/observability/sentry.py`. Initialized
  first, before logging/OTel, so it can capture setup-time errors too (same
  ordering as mitxonline/learn-ai's `settings.py`). `send_default_pii=False`
  by default, matching this service's aggregated-only-no-PII posture.
- **K8s health checks** — `/health/{startup,readiness,liveness}/`, matching
  `ol-infrastructure`'s shared `OLApplicationK8s` component's probe paths
  exactly (see `k8s/deployment.yaml`). Liveness never checks dependencies
  (a slow StarRocks shouldn't get this pod killed); readiness/startup check
  the shared StarRocks pool, extensible per-tenant via
  `core.health.register_readiness_check()`.

## Local development

```bash
uv sync
eval "$(starrocks-auth --env qa --mode vault --vault-role app --port-forward --output env)"
uv run uvicorn ol_analytics_api.main:app --reload
```

(`starrocks-auth` lives in `ol-data-platform/bin/`.)

## Tests

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```
