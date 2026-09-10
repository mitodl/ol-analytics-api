# B2B Learner Records: who authorizes a training provider

Companion to [`b2b-learner-records-design.md`](b2b-learner-records-design.md).
Status: **decided 2026-09-10.** Answers open question 3 of the
[one-pager](b2b-learner-records-onepager.md).

## Decision

Access is settled when the contract is signed. MIT does not decide it or check
it against another system at request time.

The contract with the organization says who may read its learners' records
(the organization itself, or a training provider it has contracted), and
whether names and email addresses are included. MIT then issues client
credentials that encode those terms. The partner builds the integration into
its own LMS and handles per-user authorization on its side. MIT does not model
the partner's users, and a credential reads everything its contract covers.

## How the terms are encoded

One Keycloak client per contracted integration, defined in Pulumi
(`ol-infrastructure`, `substructure/keycloak`) alongside the existing
service-account clients such as `mitxonline-b2b-client`. The reviewed change
that creates the client is MIT's record of the access.

| Contract term | On the client |
| --- | --- |
| Which organizations | A hardcoded claim (working name `learner_records_organizations`) listing the Keycloak organization UUIDs, via `keycloak.openid.HardcodedClaimProtocolMapper` |
| Identity included or not | Default client scopes: `learner-records:read` always; `learner-records:read-pii` only if the contract includes identity |
| API audience | `keycloak.openid.AudienceProtocolMapper`, as the Superset client already uses |
| Client-credentials only | `service_accounts_enabled=True`, standard flow and direct grants off |

A provider working for two organizations under two contracts holds two clients.
When a contract ends, removing its client revokes exactly that access, which
keeps the property the design already required.

Recommended (it costs one more claim): carry the contract end date as a claim
too, and have the API refuse tokens once that date has passed. A forgotten
cleanup PR then fails closed.

## What the API does

In `tenants/b2b_learner_records/auth.py`, per request:

1. Read the organization claim and scopes from the validated token.
2. If the path's `organization_id` is not in the claim, return the existing
   403, identical to the response for an organization that does not exist.
3. Populate `email` and `full_name` only when the token holds
   `learner-records:read-pii`.

No call to mitxonline or any other service, and no grant store. The client
definition is the only place access is recorded.

## Consequences

- **Open question 2 now has an owner.** Whether a provider receives learner
  identity is decided per contract and expressed as the `read-pii` scope. The
  draft OpenAPI contract's scope split already supports this unchanged.
- **Revocation is a Pulumi change.** Tokens issued before the client is removed
  stay valid until they expire. The access-token lifespan for these clients
  bounds that window. It has not been checked for the `olapps` realm yet.
- **Exports need to be covered by the same credential.** The draft says export
  files are "readable with the credentials issued alongside the API client".
  Standing storage credentials would be a second thing to revoke when a
  contract ends. Recommended: `/exports` returns short-lived presigned HTTPS
  URLs, so the API client is the only credential. Not yet applied to the
  OpenAPI draft.
- **The partner owns end-user access.** Obligations on how the partner
  restricts learner records inside its LMS belong in the contract. Nothing on
  MIT's side enforces them.

## Not chosen

- **A grant record in mitxonline, checked at request time,** with
  organization managers able to see and revoke grants. That adds a runtime
  dependency and a UI, but the decision it would store is already made in the
  contract.
- **Making the service account a member of the Keycloak organization,** so
  the existing organization-membership mapper lists it. mitxonline's org sync
  would import the service account as a learner, and it would then appear in
  the roster this API serves.

## To verify before building

- Which claim carries the client ID in a Keycloak 26.7 client-credentials
  token (`azp`, `client_id`, or both). It's needed for audit logging and rate
  limits.
- Whether APISIX's `openid-connect` plugin passes the hardcoded claim and
  scopes through in `X-Userinfo` on a bearer-only route. The learner-records
  mount needs a bearer-only route, but today's routes use the redirect flow.
  Check on QA before writing `auth.py`.
- The access-token lifespan these clients will get, since it is the
  revocation window.
