# B2B Learner Records — high-level design

**Status:** proposal · **Owner:** Data Engineering · **Consumers:** B2B site-licence organizations and their contracted training providers

## What it is

A read-only, machine-authenticated interface giving an organization — or a
training provider it has contracted — per-learner progress against its site
licence: who is on the licence, when they enrolled, when they were last active,
and whether they completed each course. It exists so partners can render learner
progress inside their own systems, or mirror it into an internal LMS.

It is deliberately not the same thing as the existing B2B analytics dashboard,
which serves aggregate, k-anonymized, org-level figures to a logged-in manager.

## Decisions

**A separate tenant, not a new endpoint on the existing one.** The existing B2B
analytics tenant is defined by not emitting individuals — a k-anonymity floor is
enforced on every row and asserted at import time. This service is individually
identifying by construction. Its authorization model is also incompatible: the
existing tenant resolves a logged-in user's manager status, and there is no user
in a machine-to-machine flow. Separate tenants cost one package and one registry
entry, and give the partner its own API document.

**Machine-to-machine auth, one client per contracted integration.** Each client
is issued its own credentials and an explicit list of organizations it may
read, covering every contract under each. A provider working under several
contracts holds one client per contract, so a contract ending revokes exactly
that client's access. Requests for an organization the client doesn't list are
refused identically to requests for organizations that do not exist, so the
interface cannot be used to enumerate MIT's customers.

**Two delivery channels over one schema.** A paged REST API for dashboards and
incremental sync, and a per-organization bulk export on the refresh cadence for
partners loading into their own systems. A batch job writes the export to S3,
outside the API. Partners read it through cross-account IAM, over SFTP, or from
a presigned URL that `/exports` returns. Same records, same field names, same
consent enforcement — the export is a second encoding of the API's schema, not
a second data product.

**Consent gates outcomes, not identity.** Organizations already hold their
learners' names and addresses; they assigned the seats. What a learner opts into
sharing is their *course status* — completion, progress, activity. So consent is
enforced on the outcome fields, and identity is governed by the contract with
the partner rather than by the learner's consent choice.

**Aggregates are exempt.** The existing k-anonymized org-level views continue to
cover the whole cohort. No consent join, no change to the aggregate models. The
floor protects small cohorts, not a small remainder: an organization holding
both the aggregates and the records could subtract one from the other to learn
a declining learner's outcome. See design §4.

**Consent enforcement fails closed.** No recorded opt-in means no outcome data.
Because suppression is per-field rather than per-record, the service can ship
before the consent field exists — every record simply reads `outcomes_shared:
false` with outcomes null, which is a degraded response rather than an empty
one, so partner integration can proceed against real records. One upstream
property is a hard requirement and cheap only if specified now: *withdrawal must
be a retained state change, not a deleted row*. A deleted consent row is
indistinguishable from one never granted and does not move the record's change
timestamp, so the withdrawal never reaches the partner holding a copy.

## Shape

Three collections, all organization-scoped and read-only: **learners** (roster
and rollup), **enrollments** (learner × course, the grain that answers "did they
complete it"), and **courses** (the contracts and course runs the identifiers
refer to), plus an **exports** manifest returning presigned URLs to the bulk
files. Incremental sync compares each record's change time with the previous
sync's `as_of`. A consent withdrawal arrives as an ordinary changed record with
outcomes nulled, and a learner who leaves the organization arrives with
`is_current: false`, so a client that upserts normally drops the data it held.

Out of scope for v1: program-level progress, per-assessment detail, any write
operation, and non-MITx-Online platforms.

### Example records

Every collection returns the same envelope, with `data` typed to its record:

```json
{
  "organization_id": "8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21",
  "as_of": "2026-08-13T06:15:00Z",
  "total_count": 47,
  "outcomes_withheld_count": 11,
  "data": []
}
```

`GET /organizations/{id}/learners`

```json
{
  "learner_id": "3e1a9c74-5b2d-4f88-9a01-7c6de2b4f019",
  "email": "rgarcia@contoso.example",
  "full_name": "R. Garcia",
  "organization_id": "8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21",
  "organization_name": "Contoso Manufacturing",
  "membership_source": "both",
  "is_current": true,
  "is_organization_manager": false,
  "first_enrolled_on": "2026-02-03T14:22:11Z",
  "last_enrolled_on": "2026-05-19T09:04:52Z",
  "courses_enrolled": 4,
  "outcomes_shared": true,
  "outcomes_consent_on": "2026-02-03T14:20:04Z",
  "last_active_on": "2026-08-11",
  "courses_in_progress": 1,
  "courses_passed": 3,
  "courses_certified": 3,
  "certificates_earned": 3
}
```

`GET /organizations/{id}/enrollments`

```json
{
  "learner_id": "3e1a9c74-5b2d-4f88-9a01-7c6de2b4f019",
  "email": "rgarcia@contoso.example",
  "full_name": "R. Garcia",
  "organization_id": "8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21",
  "contract_id": 42,
  "contract_name": "Contoso 2026 Site Licence",
  "courserun_id": "course-v1:MITxT+14.310x+2T2026",
  "courserun_title": "Data Analysis for Social Scientists",
  "courserun_start_on": "2026-02-01T00:00:00Z",
  "courserun_end_on": "2026-06-30T23:59:59Z",
  "enrolled_on": "2026-02-03T14:22:11Z",
  "enrollment_is_active": true,
  "enrollment_mode": "verified",
  "enrollment_status": null,
  "outcomes_shared": true,
  "completion_status": "certified",
  "is_passing": true,
  "grade": 0.91,
  "letter_grade": "A",
  "certificate_issued_on": "2026-07-02T11:00:00Z",
  "certificate_is_revoked": false,
  "last_active_on": "2026-08-11",
  "days_active": 34,
  "videos_watched": 212,
  "problems_attempted": 88,
  "chatbot_interactions": 14
}
```

`GET /organizations/{id}/courses`

```json
{
  "organization_id": "8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21",
  "organization_name": "Contoso Manufacturing",
  "contract_id": 42,
  "contract_name": "Contoso 2026 Site Licence",
  "contract_is_active": true,
  "contract_start_date": "2026-01-01",
  "contract_end_date": "2026-12-31",
  "seat_limit": 250,
  "courserun_id": "course-v1:MITxT+14.310x+2T2026",
  "courserun_title": "Data Analysis for Social Scientists",
  "courserun_start_on": "2026-02-01T00:00:00Z",
  "courserun_end_on": "2026-06-30T23:59:59Z"
}
```

`GET /organizations/{id}/exports`

```json
{
  "collection": "enrollments",
  "as_of": "2026-08-13T06:15:00Z",
  "format": "jsonl",
  "record_count": 163,
  "size_bytes": 214880,
  "checksum_sha256": "9f2c1b7ae4d05c8831fbb2e6a0d47c3915ee8b6042d1f7c9a3b508e2d6417f0a",
  "uri": "https://ol-b2b-exports.s3.amazonaws.com/8f14e45f/2026-08-13T06-15-00Z/enrollments.jsonl.gz?X-Amz-Expires=900&X-Amz-Signature=…",
  "uri_expires_on": "2026-08-13T09:30:00Z",
  "expires_on": "2026-09-12T06:15:00Z"
}
```

Both records above show `outcomes_shared: true`. When it is `false`, every field
from `outcomes_consent_on` onward on a learner record, and from
`completion_status` onward on an enrollment record, is `null`; identity,
contract, course-run and enrollment facts are unaffected. That is also how a
consent withdrawal arrives on the sync cursor.

## Open questions

**1. Does withholding outcomes actually protect the learner?** *Decided: out of
scope for this service.* The organization holds the full roster, so it can tell
who did not share by set difference, whatever the response reports. How an
organization may use learner data is set by its contract, which legal and
contracting own, and issuing a client presumes those terms are in place. The
record shape stays per-learner: identity intact, outcomes suppressed.

**2. Does a contracted provider receive learner identity at all?** *Decided:
yes.* The organization already holds its learners' identity (they are its
employees or students), so redacting it gains nothing. There is one read scope,
and records and exports always carry identity.

**3. Who authorizes a provider, through what workflow?** No provisioning design
exists. MIT-issued-by-ticket is simplest and leaves the organization with no
visible record of who can read its data; org self-service authorization is more
work but puts the data relationship where it belongs. This is an operational gap,
not a configuration detail.

*Decided:*
[`b2b-learner-records-provider-authorization.md`](b2b-learner-records-provider-authorization.md).
The contract settles access. MIT issues one Keycloak client per contracted
integration, carrying its organizations as a claim. The partner handles
per-user authorization in its own LMS. The contract is also the organization's
record of who can read its data: it signed the contract naming the provider, so
no organization-facing view is planned.

**4. Is consent per-organization or global?** *Decided: per `(learner,
organization)`* (design §4). A learner holding seats under two organizations can
share with one and not the other. A global flag would make withdrawal
all-or-nothing.

**5. Does access expire with the contract?** *Open, for pdpinch.* Nothing
checks a contract end date today, so a client nobody removes keeps working.
Options: an end-date claim on the client, or a warehouse check against
`dim_contract.contract_is_active`. See the
[authorization decision](b2b-learner-records-provider-authorization.md).

## Dependencies

Degrading, not blocking — the service ships without these and fills in as they
land: the learner-consent field, without which every record reads
`outcomes_shared: false`; a per-learner activity model, without which "last
active" and the engagement counters are null; and learner removal on
`mv_b2b_learner`, without which `is_current` is always true.

Blocking the first partner: the per-contract Keycloak client template and a
bearer-only gateway route. No partner can authenticate without both.
