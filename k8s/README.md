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
- the K8s namespace/Deployment/Service (can port these YAML files to Pulumi
  `kubernetes` resources directly),
- an `OLEKSAuthBinding` granting this service's K8s ServiceAccount read
  access to `database-starrocks-{env}/creds/app`,
- an `OLApisixRoute` for `/api/v1/analytics/*` with Keycloak OIDC auth
  (`OLApisixOIDCResources`) so APISIX validates the JWT and forwards
  `X-Userinfo`, matching the pattern this service's auth layer expects
  (see `src/ol_analytics_api/auth/keycloak.py`).
