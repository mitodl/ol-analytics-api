# B2B Learner Records: who authorizes a training provider

Companion to [`b2b-learner-records-design.md`](b2b-learner-records-design.md).
Status: **decided 2026-09-10.** Answers open question 3 of the
[one-pager](b2b-learner-records-onepager.md).

## Decision

Access is settled when the contract is signed. MIT does not decide it or check
it against another system at request time.

The contract with the organization says who may read its learners' records
(the organization itself, or a training provider it has contracted). MIT then
issues client credentials that encode those terms. The partner builds the integration into
its own LMS and handles per-user authorization on its side. MIT does not model
the partner's users, and a credential reads everything its contract covers.

Access is organization-wide. A client reads every learner under every contract
of each organization it lists, including learners on contracts another
provider serves. `contract_id` on the API is a filter the caller chooses, not a
limit on the credential. The contract is with the organization, and it grants
access to the organization's learners, not to one contract's cohort.

## How the terms are encoded

One Keycloak client per contracted integration, defined in Pulumi
(`ol-infrastructure`, `substructure/keycloak`) alongside the existing
service-account clients such as `mitxonline-b2b-client`. The reviewed change
that creates the client is MIT's record of the access.

| Contract term | On the client |
| --- | --- |
| Which organizations | A hardcoded claim (working name `learner_records_organizations`) listing the Keycloak organization UUIDs, via `keycloak.openid.HardcodedClaimProtocolMapper` with `jsonType.label` set to `JSON`, so the claim arrives as an array rather than a string |
| Read access | Default client scope `learner-records:read`. There is no separate identity scope; see Consequences |
| API audience | `keycloak.openid.AudienceProtocolMapper`, as the Superset client already uses |
| Client-credentials only | `service_accounts_enabled=True`, standard flow and direct grants off |

A provider working for two organizations under two contracts holds two clients.
When a contract ends, removing its client revokes that credential. Any other
client listing the same organization keeps reading the same rows, so ending one
provider's access means removing that provider's clients, not the
organization's.

**Access ends with the contract, as a backstop.** A client provisioned with
`contract_end_date` carries it as the `learner_records_contract_end_date`
claim, and the API refuses the client's tokens once that date has passed. The
date is inclusive and read as Anywhere on Earth (UTC-12): access runs through
the end of the end date wherever the partner is, and stops at 12:00 UTC the
following day. A client provisioned without an end date is open-ended. A claim
that is present but isn't a `YYYY-MM-DD` string refuses every token, since
reading it as "no end date" would fail open.

This does not replace removing the client when the contract ends. It is what
stops a client whose cleanup PR was forgotten, and when it fires the API logs
`learner_records_contract_ended` at warning level, which means that cleanup is
overdue. Every authorized request also logs the client's `contract_end_date`
beside `learner_records_access`, so an alert can raise the renewal
conversation before a partner's sync breaks.

Checking the warehouse instead (`dim_contract` has `contract_is_active` and
`contract_end_date`) was the other option. It would put a StarRocks read ahead
of every authorization decision, and a client covers every contract under
each of its organizations, so no single `dim_contract` row says when the
client's access ends.

## What the API does

In `tenants/b2b_learner_records/auth.py`, per request:

1. Read the organization claim and scopes from the validated token. A missing
   claim, or one that isn't an array of UUIDs, lists no organizations, as
   `b2b_dashboard/auth.py` already treats a malformed organization claim.
2. If the path's `organization_id` is not in the claim, return the existing
   403, identical to the response for an organization that does not exist.
3. Require the `learner-records:read` scope. Identity fields are always
   populated.

Before any of that, `token.py` verifies the token and refuses one whose
contract end date has passed (above).

No call to mitxonline or any other service, and no access store. The client
definition is the only place access is recorded.

## Consequences

- **No identity split.** The organization already holds its learners' names
  and addresses (they are its employees or students), so redacting them gains
  nothing. The draft's `read-pii` scope is dropped. Records and exports always
  carry identity, and one export set per organization serves every client. This
  settles open question 2.
- **Revocation is a Pulumi change.** Tokens issued before the client is removed
  stay valid until they expire. The access-token lifespan for these clients
  bounds that window. It has not been checked for the `olapps` realm yet.
- **Exports are written out of band and reached three ways.** A batch job
  writes each organization's export to its own prefix in S3. A partner reads it
  through a cross-account IAM role limited to that organization's prefix (no
  `ListBucket` beyond it), over SFTP, or through `/exports`, which returns a
  short-lived presigned HTTPS URL to the same file. The IAM role and SFTP
  account are credentials separate from the API client, so ending a contract
  means revoking those too. `/exports` needs only the API client.
- **The partner owns end-user access.** Obligations on how the partner
  restricts learner records inside its LMS belong in the contract. Nothing on
  MIT's side enforces them.
- **The contract is the organization's record of who reads its data.** The
  organization signed the contract that names the provider, so it already
  knows. No organization-facing view of clients is planned.

## Not chosen

- **An access record in mitxonline, checked at request time,** with
  organization managers able to see and revoke access. That adds a runtime
  dependency and a UI, but the decision it would store is already made in the
  contract, which is also where the organization sees it.
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
  Check on QA. This no longer gates the tenant (see below), but the claim
  still has to arrive in the token.
- The access-token lifespan these clients will get, since it is the
  revocation window.

## The app verifies the token; the gateway is not the trust boundary

Settled 2026-09-18, after Copilot raised it on
[ol-infrastructure#5939](https://github.com/mitodl/ol-infrastructure/pull/5939).

The question above was whether APISIX overwrites a caller-supplied
`X-Userinfo`. It does, at the start of its `rewrite` phase. That is beside the
point, because a caller does not have to go through APISIX. The pod security
group admits the whole pod subnet, and `aws-eks-nodeagent` runs with
`--enable-network-policy=false` on both data clusters, so the NetworkPolicies
that exist are no-ops. Any compromised in-cluster workload can post a forged
`X-Userinfo` naming `learner-records:read` and any organization UUID, straight
to the pod.

For k-anonymized aggregates that is a risk worth arguing about. For records
that name individual learners it is not, so this tenant verifies the bearer
token itself (`tenants/b2b_learner_records/token.py`): RS256 against the
realm's JWKS, checking issuer, audience and lifetime, and taking `scope`,
`learner_records_organizations` and `azp` from the verified payload.
`X-Userinfo` is not read at all. The gateway route stays as it is; it is now
defence in depth rather than the only check.

Rejected: locking down the pod security group, and enabling CNI network
policy cluster-wide. Both are larger changes that protect one tenant by
changing how every workload on the cluster is reached.

`b2b_dashboard` still authorizes from `X-Userinfo`. Its exposure is aggregate
and k-anonymized and it has a browser session flow, so it is a separate
decision, not a follow-up to this one.

## Follow-ups

Not needed to write the tenant, but each needs an owner before partners
multiply:

- **Client lifecycle.** One client per contract covers creation only. Who
  delivers the secret to the partner, who rotates it, and who removes it when
  the contract ends are unassigned.
- **Audit logging, before the first partner.** `auth.py` logs the client ID and
  organization on every authorized request. It doesn't log how many records
  were returned, and nothing yet says where those logs are kept or who reviews
  them.
- **Rate limits.** The contract documents a 429 but no limits, and hasn't
  decided whether APISIX or the app enforces them.
