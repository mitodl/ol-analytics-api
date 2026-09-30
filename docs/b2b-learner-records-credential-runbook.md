# B2B Learner Records: partner credential runbook

How a partner gets a learner-records API credential, how it is rotated, and how
it is taken away. Companion to
[`b2b-learner-records-provider-authorization.md`](b2b-learner-records-provider-authorization.md),
which explains why access is encoded this way.

Each credential is a Keycloak client-credentials client in the `olapps` realm,
defined by one entry in the `ol-substructure-keycloak` stack config
(`ol-infrastructure`, `substructure/keycloak/learner_records.py`). That entry is
the only record of what the partner can read. Adding it grants access, removing
it revokes access, and the PR that changes it is the audit trail.

## Before issuing: the opt-in and the contract

Not every contracted organization gets a credential. The organization opts in
to this integration, and that opt-in is its own recorded decision. Don't infer
it from a contract existing, because the credential reads identifiable learner
records (email and full name are always returned).

The opt-in is recorded on the organization's contract in MITx Online. That
field doesn't exist yet. Until it does, the PR that adds the client must link
the organization's written request (email or ticket).

The contract has to state:

- Which organizations the partner reads. Access is organization-wide: a client
  reads every learner under every contract of each organization it lists, not
  only one contract's cohort.
- The end date.
- The partner's obligations for restricting learner records inside its own LMS.
  Nothing on MIT's side enforces these.

A provider working for two organizations under two contracts gets two clients.

## Issuing a credential

1. Collect the terms for each environment. Organization UUIDs are the
   organization's `sso_organization_id` in MITx Online (the Keycloak
   organization id), and they differ between QA and Production. The end date is
   the contract's `contract_end` in MITx Online.
2. Open a PR against `ol-infrastructure` adding an entry to
   `keycloak_realm:olapps-learner-records-clients` in
   `substructure/keycloak/Pulumi.<Env>.yaml`:

   ```yaml
   keycloak_realm:olapps-learner-records-clients:
     - name: contoso-lms
       description: Contoso LMS integration for Contoso Manufacturing
       organizations:
         - 8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21
       contract_end_date: "2027-06-30"
   ```

   `name` becomes the client id `learner-records-<name>` and the Vault path.
   Always set `contract_end_date` for a partner. It is optional in the schema
   only because internal test clients have no contract.

   Anyone can file the PR. Platform engineering reviews it and checks that the
   opt-in is linked, that the UUIDs belong to the right organizations in that
   environment, and that the end date matches the contract.
3. Merge, then deploy. In the `docker-packer-pulumi-keycloak` Concourse
   pipeline, only CI deploys on merge. QA and Production each wait on a
   `[bot] Pulumi ol-substructure-keycloak <Stack> ready to deploy.` GitHub
   issue, and closing it deploys that stack (Production only after QA). Close
   the QA gate, then the Production gate, and confirm the
   `deploy-ol-substructure-keycloak-<stack>` job succeeded. A gate deploys the
   newest previewed commit, so check its preview for unrelated Keycloak changes
   before closing it.
4. The apply writes the credential to Vault at
   `secret-operations/sso/learner-records/<name>`: `client_id`,
   `client_secret`, `issuer`, `token_url`, `scope`, `organizations` and
   `contract_end_date`. That document is what the partner receives. The API
   itself has no read access to this path.
5. Smoke-test before handing it over. This keeps the secret out of files and
   out of process arguments:

   ```bash
   CREDS=$(vault read -format=json secret-operations/sso/learner-records/<name>)
   TOKEN=$(jq -r '.data | "grant_type=client_credentials&client_id=\(.client_id)&client_secret=\(.client_secret)"' <<<"$CREDS" \
     | curl -s --data @- "$(jq -r .data.token_url <<<"$CREDS")" | jq -r .access_token)
   curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
     "https://analytics-qa.ol.mit.edu/api/v1/learner-records/organizations/<org uuid>/courses"
   unset CREDS TOKEN
   ```

   Expect `200`. Decoding the token should show `learner_records_organizations`
   as a JSON array and `learner-records:read` in `scope`. The Production host
   is `analytics.ol.mit.edu`.

## Handing the credential to the partner

Send the Vault document to the partner's named technical contact, in this
order of preference. Staff with read access to
`secret-operations/sso/learner-records/` perform the handoff.

1. Vault response wrapping. Wrap the document in a single-use token:

   ```bash
   vault read -format=json -wrap-ttl=72h \
     secret-operations/sso/learner-records/<name> | jq .wrap_info
   ```

   Send the partner `token` and tell them the Vault address, the TTL and the
   expected creation path. Keep `accessor` in the handoff record. If the
   partner reports a failed unwrap before the TTL is up,
   `vault token lookup -accessor <accessor>` tells you which case it is: the
   lookup succeeds if the wrapping token is still unused (the partner has the
   wrong token), and fails if it was already unwrapped.

   The partner checks the token before using it, then unwraps it once:

   ```bash
   curl -s -X POST -d '{"token": "<wrapping token>"}' \
     https://vault-production.odl.mit.edu/v1/sys/wrapping/lookup
   # creation_path must be secret-operations/sso/learner-records/<name>
   curl -s -X POST -H "X-Vault-Token: <wrapping token>" \
     https://vault-production.odl.mit.edu/v1/sys/wrapping/unwrap
   ```

   A wrong `creation_path` means the token was swapped in transit. A failed
   unwrap on a token that hasn't expired means someone else unwrapped it
   first. In both cases, rotate the secret (below) and send it again. The
   Vault hostname resolves publicly, but unwrapping from outside MIT's network
   hasn't been tried with a partner yet. If the partner can't reach it, use the
   next option.
2. A LastPass share to the partner contact. Remove the share once they confirm
   receipt.
3. GPG-encrypted email to the partner contact's public key. Confirm the key
   fingerprint with them over a separate channel first.

Tell the partner:

- Access tokens last 300 seconds. Request a new one with the client-credentials
  grant as needed.
- The client is removed on the contract's end date. Once
  [ol-analytics-api#73](https://github.com/mitodl/ol-analytics-api/pull/73)
  merges, the API also refuses its tokens from 12:00 UTC on the day after the
  end date, whichever comes first.
- Who to contact to rotate the secret or report it exposed.

## How fast a change takes effect

Every change below lands when its stack deploys, not when the PR merges, and
QA and Production deploy only once their gates are closed (see step 3 above).
After that, tokens already issued keep working until they expire: 300 seconds
(`ACCESS_TOKEN_LIFESPAN_SECONDS`, pinned on each client so a realm-wide change
can't widen it), plus the API's 30-second clock leeway once
[ol-analytics-api#69](https://github.com/mitodl/ol-analytics-api/pull/69)
merges. The API verifies tokens against the realm signing keys and doesn't ask
Keycloak whether the client still exists, so there is no way to cut off a
token already issued. For an exposure or an early termination, close the gates
as soon as the PR merges.

## Rotating a secret

Rotate on suspected exposure, when the partner's staff with access change, or
when the partner asks. No routine rotation interval has been set.

1. Increment `secret_version` on the client's entry (it defaults to `1`) in a
   PR. On deploy, Keycloak issues a new secret, the old one stops working, and
   the Vault document is updated in the same apply.
2. For a planned rotation, agree the deploy window with the partner, since
   their integration fails from the deploy until they have the new secret. For
   an exposure, don't wait.
3. After the deploy, hand the new secret over as above.

## Revoking access

Removing the client's entry from the stack config deletes the Keycloak client
and its Vault document.

- Planned end: open the removal PR before the end date, and merge and deploy it
  on the day. Once #73 merges, every `learner_records_access` log line carries
  the client's `contract_end_date`, so a Loki alert can warn ahead of a lapse.
  That alert rule doesn't exist yet.
- Early termination: the same removal PR, deployed immediately.
- An organization dropping out of a multi-organization client: remove its UUID
  from `organizations`. Tokens issued before the deploy still list it until
  they expire. The other organizations keep working.
- Backstop: once #73 merges (it is stacked on #69), the API refuses a token
  whose `learner_records_contract_end_date` has passed, from 12:00 UTC on the
  day after the end date (the end date is inclusive and read as Anywhere on
  Earth). It logs `learner_records_contract_ended` at warning level when it
  does. That means a client outlived its contract, so remove it now. Until #73
  merges there is no backstop, and a forgotten client keeps reading.

When the bulk export channel exists, a partner's cross-account IAM role and
SFTP account are separate credentials and have to be revoked alongside the
client.

## QA credentials for partner integration testing

A partner building against QA gets its own client in `Pulumi.QA.yaml`, pointed
at a QA organization of synthetic learners, and receives it the same way from
`vault-qa.odl.mit.edu`. QA credentials never list a real organization's UUID.
