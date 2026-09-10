# B2B Learner Records: who authorizes a training provider

Companion to [`b2b-learner-records-design.md`](b2b-learner-records-design.md).
Status: **proposal, pending product-owner sign-off.** Answers open question 3 of
the [one-pager](b2b-learner-records-onepager.md). Questions 1, 2 and 4 stay
open; §6 notes how this changes question 2.

## 1. The question

A contracted training provider calls the API with its own client credentials
and reads individually identifying records for an organization's learners.
Something has to say "this provider may read this organization's learners, on
these terms, until this date". The question is who says it and where it is
recorded.

This is what gates onboarding the first provider. The rest of the service
(tenant, dbt models, consent field) can be built without it.

## 2. What exists today

Nothing models the relationship. There is no provider, partner or API-client
record attached to an organization or contract in any system.

| Piece | Where it lives today | Relevance |
| --- | --- | --- |
| Organization | mitxonline `b2b.models.OrganizationPage`, linked to Keycloak by `sso_organization_id` | The grant's subject. The API's `organization_id` is this UUID. |
| Org manager | mitxonline `b2b.models.UserOrganization.is_manager` | Set only in Django admin (`UserOrganizationAdmin`), row by row, by MIT staff. Never reaches the Keycloak token. |
| Manager designation, future | Keycloak Organization Groups, per mitodl/hq#10594. Labelled C4 in the capability glossary of the provisioning spec (mitodl/mitxonline#3922), from the onboarding RFC mitodl/hq#12784 | Gives the organization a manager role it controls, with an audit trail. Not built. |
| Service-account clients | Pulumi, `ol-infrastructure` `substructure/keycloak/olapps.py` (e.g. `mitxonline-b2b-client`), scopes via `ClientDefaultScopes` | The pattern for issuing a provider's credential. |
| Ownership split | mitxonline PR #3922, `docs/source/b2b/provisioning_api.md` | Pulumi keeps realm, flows, client scopes, **clients** and service-account grants. The mitxonline provisioning API owns everything per customer. |
| Token validation | APISIX in front of ol-analytics-api; the app decodes `X-Userinfo` (`core/auth/userinfo.py`) | No inbound client-credentials principal exists yet. |
| Service-to-service check | `b2b_dashboard` asks mitxonline whether a user is a manager with its own client-credentials token, scope `b2b:manager-check` (`tenants/b2b_dashboard/mitxonline_client.py`, mitxonline `b2b/views/v0/service.py`) | The pattern for checking a grant at request time. |
| Manager UI | mit-learn contract admin page, `app-pages/ContractAdminPage`, which already has a revoke/confirm flow for seat codes | Where an organization would see and revoke grants. |

## 3. Separate the credential from the grant

The two shapes in the original question ("MIT issues credentials by ticket" vs
"the org authorizes from its dashboard") conflate two different objects:

- **The credential** identifies the provider. One Keycloak client per provider,
  whatever the number of organizations it works for. It carries no
  organization. Issuing it is rare, provider-level, and fits the onboarding
  split: clients are substrate, so they stay in Pulumi under review.
- **The grant** is the relationship: provider X may read organization Y's
  learners, with or without identity, until a date. It is per customer, it
  changes whenever a contract starts or ends, and it is the thing the
  organization needs to see. Under the onboarding split it belongs in
  mitxonline, next to `OrganizationPage` and `ContractPage`.

A credential without a grant reads nothing: every request returns the same 403
as an unknown organization. So issuing credentials can stay an MIT operations
step without MIT deciding who reads whose data.

## 4. Recommendation

**The organization authorizes. Never the provider, and never MIT alone.** MIT
records and enforces the organization's decision.

### Phase 1: staff-recorded grants, org-visible, org-revocable

Enough to onboard the first provider.

1. The provider gets a Keycloak client from a reviewed Pulumi change. It holds
   no grants.
2. The organization authorizes the provider in writing. MIT confirms with the
   organization's contact of record, not a contact the provider supplies. That
   closes the obvious social-engineering route of a provider asking MIT for
   access on the organization's behalf.
3. MIT staff record the grant in mitxonline (Django admin), with the
   authorizing person and a reference to the written authorization.
4. The organization's managers see active and past grants on the mit-learn
   contract admin page, and can **revoke** one there.

Self-service revocation ships in phase 1. Self-service authorization waits for
phase 2, and the asymmetry is deliberate. Revoking can only reduce disclosure,
so the current manager flag (set by MIT staff in Django admin) is authority
enough to act on it. Authorizing increases disclosure of individually
identifying data. That should rest on a manager role the organization controls
and that leaves an audit trail, which is what C4 provides.

The visible grant list is what separates this from "MIT-issued-by-ticket". The
objection to the ticket-only shape was that the organization has no record of
who can read its learners' data. Phase 1 fixes that without building a
self-service authorization flow.

### Phase 2: organization self-service authorization

Blocked by C4. The provider (or MIT staff on its behalf) creates a *pending*
grant naming the organization, and an organization manager approves it in
mit-learn. The provider can request but never activate. MIT staff keep the
ability to revoke, but no longer record authorizations.

## 5. The grant record

A mitxonline model in the `b2b` app. Working name `OrganizationDataGrant`.

| Field | Note |
| --- | --- |
| `organization` | FK `OrganizationPage`. |
| `client_id` | The provider's Keycloak client ID. |
| `provider_name` | Display name for the manager UI. |
| `includes_identity` | Whether records carry `email` and `full_name`. See §6. |
| `status` | `pending`, `active`, `revoked`. |
| `expires_on` | Required. Not later than the end of the organization's latest active contract. |
| `authorized_by_name`, `authorized_by_email` | The organization-side person who authorized it. |
| `authorization_reference` | Ticket or document reference for the written authorization (phase 1). |
| `recorded_by`, `activated_on` | MIT staff user (phase 1) or the approving manager (phase 2). |
| `revoked_by`, `revoked_on`, `revocation_reason` | |

Two rules, the same ones the design already imposes on consent:

- **Rows are never deleted.** Revocation is a retained state change. A deleted
  grant cannot answer "who could read our data in March", which is the first
  question an organization asks after a dispute with a provider.
- **Expiry is computed at check time,** so a grant is active only if
  `status = active` and `expires_on` is in the future. A contract ending needs
  no job to revoke access.

## 6. How the API checks a grant

Per request, in `tenants/b2b_learner_records/auth.py`:

1. Read the client ID from the validated token.
2. Resolve the client's active grants from mitxonline through a new service
   endpoint. Use the same pattern as `b2b:manager-check`, under a new scope
   (e.g. `b2b:data-grant-check`). Cache per client for a short TTL (proposed
   60 s).
3. If there is no active grant for the path's `organization_id`, return the
   existing 403, identical to the one for an organization that does not exist.
4. If mitxonline is unreachable and the cache is expired, return 503. Never
   serve from a stale grant list, because that would keep a revoked grant
   readable.

Revocation takes effect within one TTL.

### Identity moves onto the grant

The draft contract gates `email`/`full_name` on the client scope
(`learner-records:read` vs `learner-records:read-pii`). A scope belongs to the
client, and a provider working for two organizations has one client, so a
scope cannot express "identity for A, not for B". Proposed rule: identity is
returned only when **both** the client holds `learner-records:read-pii` **and**
the grant has `includes_identity`. The scope becomes MIT's ceiling for that
provider, and the grant flag is the organization's choice within it.

This reframes open question 2. Instead of asking whether a provider's
credential gets PII, it asks whether MIT ever sets the PII ceiling for
providers, and what `includes_identity` defaults to. It still needs a policy
owner.

### Exports need the same gate

The draft says export files are "readable with the credentials issued
alongside the API client". Those would be standing S3/SFTP credentials outside
the grant check, so revoking a grant would leave the export readable. Proposed:
`/exports` returns short-lived presigned HTTPS URLs, minted per request after
the grant check. No standing storage credentials in v1. A push to a
partner-owned SFTP endpoint can come later, keyed off the same grant.

## 7. Rejected alternatives

- **Grant as Keycloak client configuration** (a hard-coded claim listing
  organization IDs, or per-organization client scopes). This puts a
  per-customer resource on a Pulumi-owned client, contrary to the onboarding
  split. It makes every grant change an infrastructure deploy, and the
  organization still sees nothing.
- **Service account as a member of the Keycloak organization,** so the
  existing organization-membership mapper lists the organization in the token.
  It reuses machinery, but mitxonline's org sync would pull the service account
  in as a learner. It would then appear in `bridge_user_organization`, the
  roster this API serves.
- **Grants landed in the warehouse and joined like consent.** Revocation would
  lag the materialized-view refresh (manual, Dagster-driven), and prompt
  revocation is the one property a grant must have.
- **Provider self-service.** The provider is the party whose access is in
  question.

## 8. To verify before building

- Which claim carries the client ID in a Keycloak 26.7 client-credentials
  token (`azp`, `client_id`, or both), and whether APISIX's `openid-connect`
  plugin populates `X-Userinfo` with it for a bearer-only route. The
  learner-records mount needs a bearer-only route; today's routes use the
  redirect flow. Check on QA before writing `auth.py`.
- Whether `ContractAdminPage` is the right home for the grant list, or whether
  it belongs at organization level. A grant spans contracts.

## 9. Work this creates

| Repo | Work | Phase |
| --- | --- | --- |
| mitxonline | `OrganizationDataGrant` model, Django admin, service endpoint + OAuth scope, manager list/revoke endpoints | 1 |
| mit-learn | Grant list and revoke action for organization managers | 1 |
| ol-infrastructure | Per-provider Keycloak client template; bearer-only APISIX route for `/api/v1/learner-records`; new service scope on the ol-analytics-api client | 1 |
| ol-analytics-api | Grant resolution with TTL cache, fail-closed; identity rule; presigned export URLs | 1 |
| mitxonline, mit-learn | Pending grants, manager approval | 2, blocked by C4 |
| ol-analytics-api | OpenAPI changes from §6 (PII rule text, export `uri` semantics) | on sign-off |
