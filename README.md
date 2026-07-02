# ol-analytics-api

Standalone read-only FastAPI service serving aggregated B2B site-license
analytics (contract utilization, enrollment/completion funnel, monthly
engagement trend, program funnel, content engagement depth, MIT-admin
contract health) to the MIT Learn dashboard at
`/dashboard/organization/[orgSlug]/analytics`.

Aggregated-only — no individual learner PII is served by this API.

## Architecture

```
dbt (organization_administration_report, Iceberg)
  -> StarRocks materialized views (ol-data-platform, models/b2b_analytics/*.sql)
  -> this service (reads StarRocks over MySQL wire protocol via aiomysql)
  -> MIT Learn Next.js dashboard (React Query + Recharts)
```

Auth: APISIX validates the Keycloak JWT in front of this service and
forwards decoded claims as a base64-encoded JSON blob in the `X-Userinfo`
header (see `src/ol_analytics_api/auth/keycloak.py`) — this service does not
validate tokens or fetch JWKS itself. Org-manager checks round-trip to MITx
Online (`src/ol_analytics_api/auth/mitxonline_client.py`); MIT-admin checks
use a Keycloak realm role (`src/ol_analytics_api/auth/dependencies.py`). See
hq#10594 for the full auth design.

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
