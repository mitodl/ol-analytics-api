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
                              # /health + /ready, TENANTS registry, mounts each sub-app
  core/                       # shared by every tenant, no tenant-specific policy
    config.py                 # StarRocks host/port + Vault K8s-auth wiring
    db/client.py               # aiomysql connection pool (one pool, all tenants)
    db/vault_credentials.py    # dynamic StarRocks creds via Vault K8s auth
    db/identifiers.py          # SQL-identifier validation for schema names spliced into queries
    auth/userinfo.py           # generic X-Userinfo decode (APISIX forwards this to every tenant)
    anonymization.py           # generic k-anonymity-style row suppression, floor is a tenant param
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
state. Wire it up with one line in `main.py`'s `TENANTS` list:

```python
TENANTS: list[tuple[str, FastAPI]] = [
    ("/api/v1/analytics", b2b_dashboard_app),
    ("/api/v1/<new-tenant>", new_tenant_app),
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
