# K8s manifests

`namespace.yaml`, `deployment.yaml`, `service.yaml` here are for local
review/dry-run (`kubectl apply --dry-run=client`) and as a reference for the
real Pulumi-managed deployment.

This org's other K8s services (e.g. `learn_ai`, `mit_learn`) are **not**
deployed from raw manifests — they're provisioned via Pulumi in
`ol-infrastructure`, under `src/ol_infrastructure/applications/`, using the
`OLApisixRoute` / `OLApisixOIDCResources` components
(`src/ol_infrastructure/components/services/apisix.py`) for ingress + auth,
and `OLEKSAuthBinding` for the Vault Kubernetes-auth role binding referenced
by this Deployment's `serviceAccountName`.

Follow-up work (tracked under the FastAPI Service Scaffold & Infrastructure
epic, not done in this repo): add an `applications/ol_analytics_api/__main__.py`
Pulumi program in `ol-infrastructure` modeled on `applications/learn_ai/__main__.py`,
wiring:
- Ideally the `OLApplicationK8s` component
  (`src/ol_infrastructure/components/services/k8s.py`) rather than hand-rolled
  Deployment/Service resources — it already hardcodes the
  `/health/{startup,readiness,liveness}/` probe paths and timings this
  service's `core/health.py` matches, so using it directly avoids the drift
  risk of maintaining a second copy of those probe configs.
- an `OLEKSAuthBinding` granting this service's K8s ServiceAccount read
  access to `database-starrocks-{env}/creds/app`,
- an `OLApisixRoute` for `/api/v1/analytics/*` with Keycloak OIDC auth
  (`OLApisixOIDCResources`) so APISIX validates the JWT and forwards
  `X-Userinfo`, matching the pattern this service's auth layer expects
  (see `src/ol_analytics_api/core/auth/userinfo.py`),
- `merge_otel_resource_attributes()` (`ol_infrastructure/lib/pulumi_helper.py`)
  for `OTEL_RESOURCE_ATTRIBUTES`, which is how `GIT_SHA`/`HOSTNAME` actually
  get substituted in production — `deployment.yaml` in this repo has those as
  literal `${...}` placeholders since there's no real deploy pipeline wiring
  them yet.
- a Vault-sourced `SENTRY_DSN` (see `secret-operations/sso/...`-style secret
  paths used elsewhere — the exact path isn't decided yet for this service).
