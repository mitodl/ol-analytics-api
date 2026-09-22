# ol-analytics-api

Multi-tenant, read-only FastAPI analytics gateway over StarRocks. Each
consumer of this service is a **tenant**: an independent FastAPI sub-app
with its own routes, auth/governance model, and OpenAPI docs, mounted onto
one root app. Tenants share no application state — routers, auth, config,
and suppression policy are all per-tenant.

They do, however, currently share one StarRocks connection pool, and that
pool authenticates as a single DB identity (the `app` Vault role) with one
privilege set. So "tenant" is today an **application-layer** boundary, not a
database-enforced one: nothing at the StarRocks level stops one tenant's
queries from reading another tenant's schema — only the app-layer routing and
each tenant's own `starrocks_schema` do. Making `tenant` a real security
boundary (per-tenant Vault role / StarRocks user with schema-scoped grants,
and per-tenant pools here) is tracked as follow-up work; see the
tenant-isolation task in the architecture-review epic.

The first tenant is `b2b_dashboard`: aggregated (no individual learner PII)
B2B site-license analytics — contract utilization, enrollment/completion
funnel, monthly engagement trend, program funnel, content engagement depth,
MIT-admin contract health — for the MIT Learn dashboard at
`/dashboard/organization/[orgSlug]/analytics`.

The second is `b2b_learner_records`: identifiable per-learner roster,
enrollment and completion records for B2B site-license partners and the
training providers they contract, over machine-to-machine client credentials.
It has the opposite privacy posture, so it shares no auth, models or
suppression code with `b2b_dashboard`. Design and contract:
`docs/b2b-learner-records-design.md` and
`docs/openapi/b2b-learner-records-v1.yaml`.

## Architecture

```
dbt (organization_administration_report, Iceberg)
  -> StarRocks materialized views (ol-data-platform, models/b2b_analytics/*.sql)
  -> this service:
       main.py (shared StarRocks pool, mounts tenant sub-apps)
         -> tenants/b2b_dashboard        (mounted at /api/v1/analytics)
         -> tenants/b2b_learner_records  (mounted at /api/v1/learner-records;
                                          reads models/b2b_learner_records/*.sql)
         -> tenants/<next-tenant>        (mounted at its own prefix)
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
    anonymization.py           # k-anonymity floor per model CohortPolicy: drop sub-floor rows, null sub-floor secondary counts + derivatives
    observability/
      processors.py             # structlog trace_id/span_id + k8s pod/namespace injection
      logging.py                 # structlog config: JSON in prod, console in dev
      telemetry.py                # OpenTelemetry SDK + auto-instrumentation (traces)
      sentry.py                   # Sentry init
      middleware.py                # structured per-request access log, shared by every app instance
  tenants/
    b2b_dashboard/
      app.py                   # FastAPI() sub-app instance, includes this tenant's routers
      config.py                # this tenant's policy: schemas, MITx Online URL, admin role, floor, consent_fail_open
      auth.py                  # this tenant's governance gates (require_org_manager, require_mit_admin)
      mitxonline_client.py     # service-authenticated org-manager check against MITx Online
      models.py                # SQLModel response schemas for this tenant's 6 MVs
      learner_queries.py       # learner-progress SQL; no anonymization floor, consent-gated outcomes
      learner_models.py        # LearnerProgress — no CohortPolicy
      routers/
        organizations.py       # relative paths — mount point supplies the /api/v1/analytics prefix
        contracts.py
        learners.py            # /organizations/{id}/contracts/{id}/learner-progress
        admin.py
    b2b_learner_records/
      app.py                   # sub-app; contract-shaped 400s for malformed parameters
      config.py                # schema (b2b_learner_records), page caps, consent_fail_open
      auth.py                  # client-credentials org grant + learner-records:read scope
      queries.py               # SQL templates; consent enforcement (fails closed unless toggled)
      models.py                # Learner, Enrollment — no CohortPolicy, outcome fields consent-gated
      routers/
        organizations.py       # /organizations/{id}/learners, /enrollments
```

### Adding a new tenant

A new consumer — a different internal tool, a partner integration, a public
read-only feed — gets its own package under `tenants/`, following the same
shape as `b2b_dashboard/`: an `app.py` exposing a `create_app()` factory, its
own `config.py`/`auth.py`/`routers/`. It can define completely different
auth (API keys, no auth, a different Keycloak realm role), a different
StarRocks schema, and its own suppression policy — none of that is shared
state. Wire it up with one entry in `main.py`'s `TENANTS` list:

```python
TENANTS: list[Tenant] = [
    Tenant(
        b2b_dashboard.TENANT_NAME,
        "/api/v1/analytics",
        b2b_dashboard.create_app,
        b2b_dashboard.lifespan,
    ),
    Tenant(new_tenant.TENANT_NAME, "/api/v1/<new-tenant>", new_tenant.create_app),
]
```

A `Tenant` takes a `create_app` *factory* (not a pre-built instance) so the
root app constructs every sub-app after OpenTelemetry is configured — a
tenant is instrumented regardless of import order. If the tenant owns
resources that need startup/shutdown (e.g. an httpx client), it exposes them
as an ordinary `lifespan` context manager and passes it as the fourth
argument: a mounted sub-app's own `lifespan=` is never invoked by the ASGI
server (only the root app's is), so the root lifespan enters each tenant's
explicitly.

The leading `name` is the tenant's own `TENANT_NAME`, which already names its
readiness sub-path. It also names the tenant's published OpenAPI document
(`openapi/specs/<name>.yaml`), so it ends up in a consumer-visible filename.

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

`b2b_learner_records` has no user in the flow. Each contracted integration
gets its own Keycloak client-credentials client, and the organization UUIDs
its contract covers ride in a hardcoded `learner_records_organizations` claim
(a JSON array). `tenants/b2b_learner_records/auth.py` requires the
`learner-records:read` scope and the path's organization in that claim, and
refuses identically whether the organization is ungranted or doesn't exist.
There's no round-trip and no grant store; removing the client revokes the
access. See `docs/b2b-learner-records-provider-authorization.md`.

That tenant does **not** read `X-Userinfo`. It verifies the bearer token
itself (`tenants/b2b_learner_records/token.py`): RS256 against the realm's
JWKS, checking issuer, audience, token class and lifetime, and it takes the
claims it authorizes on from the verified payload. The gateway rebuilds
`X-Userinfo` only for traffic that goes through the gateway, and the pod is
reachable without doing so — the pod security group admits the whole pod
subnet and the CNI runs with network policy disabled on both data clusters.
Aggregate k-anonymized figures can live with that; records naming individual
learners can't. `OL_ANALYTICS_API_B2B_LEARNER_RECORDS_ISSUER` and
`..._AUDIENCE` come from Vault via the Pulumi stack, out of the same entry
the gateway route reads, so the two can't check different realms. Both
default to production's, and the app refuses to start if the issuer is left
at that default in any other deployed environment.

The org-manager round-trip authenticates with this service's **own** OAuth2
client-credentials token and names the subject user explicitly
(`?user_global_id=<keycloak sub>`), rather than forwarding the caller's
identity. It has to: APISIX deliberately strips client-supplied
`X-Userinfo`/`X-Access-Token` headers before they reach an upstream, so a
forwarded identity never survives the gateway. `OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_ID`
and `..._CLIENT_SECRET` come from Vault via the Pulumi stack; both are empty
by default so local dev and the test suite run without credentials.

The `is_manager` flag is curated only in MITx Online's Django admin and
never reaches the Keycloak token, which is the sole reason this round-trip
exists. Once org-manager status is visible in Keycloak (hq#10594) the
round-trip and `mitxonline_client.py` both go away.

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
  tenant request twice. Granian's own access log stays off (its default) to
  avoid duplicating this in a different, unstructured format.
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
uv run granian --interface asgi --reload ol_analytics_api.main:app
```

(`starrocks-auth` lives in `ol-data-platform/bin/`.)

## Tests

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

## The published API contract

Each tenant's OpenAPI document is committed under `openapi/specs/<tenant>.yaml`
and regenerated with:

```bash
uv run bin/generate-openapi-spec
```

Run it whenever a response model, route or query parameter changes. CI fails
otherwise — both as a test (`tests/test_openapi_spec.py`) and as a
`--check` run of the generator itself.

The spec is committed rather than served-and-forgotten because it is meant to
become a cross-repo interface. The intended pipeline mirrors the one already
running for `mitxonline` and `mit-learn`: a Concourse pipeline in
`ol-infrastructure` (`ol_concourse/pipelines/libraries/api_clients_pipeline.py`)
watching these files on a release branch, running `openapi-generator` over
them, and publishing a TypeScript client the same way
`@mitodl/mitxonline-api-axios` and `@mitodl/mit-learn-api-axios` are today.
None of that is wired up yet — this repo has no entry in `PIPELINE_CONFIGS`
and no `release` branch, and MIT Learn's dashboard still uses its hand-written
client. Until it is, committing the spec still buys the same thing locally: a
column that appears here without appearing in the diff is a column a
consumer would find out about at runtime once the pipeline exists.

Three details are worth knowing before editing a route:

- **`operation_id` is named explicitly on every route.** It becomes the
  generated client's method name, so FastAPI's path-derived default would both
  produce an unreadable name and rename the method whenever the path moves.
- **Published paths carry the tenant's mount prefix.** A mounted sub-app
  describes its routes relative to its own root; `openapi.py` re-prefixes them
  so a generated client configured with the service host requests the URLs the
  service actually serves.
- **A repeatable query parameter is a plain `list[X]`, never `list[X] | None`.**
  The optional form renders as `anyOf: [array, null]`, which openapi-generator
  cannot reduce; it emits a client that spreads the value with `Object.entries`
  and sends `?0=a&1=b` instead of repeating the parameter name. Use
  `Query(default_factory=list)` and treat the empty list as "no filter".

Note that `docs/openapi/b2b-learner-records-v1.yaml` is a different artifact:
a hand-written draft published so partners could review the record shape
before it was built. `openapi/specs/b2b_learner_records.yaml` is generated
from the running code and is the one a client is built from.
