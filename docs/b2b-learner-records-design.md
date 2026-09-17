# B2B Learner Records — data backing, scope, and tenancy

Companion to [`openapi/b2b-learner-records-v1.yaml`](openapi/b2b-learner-records-v1.yaml).
Status: proposal. Nothing here is implemented.

## The ask

An M2M-authenticated API giving a B2B partner (or a training provider they
contract) a per-learner view of progress against their site licence: who is on
the licence, when they enrolled, when they were last active, whether they
completed each course.

## 1. Does the data exist?

Mostly. Every identity, enrollment and completion field is available in the
dimensional layer today. **Activity — including "last active", the field the ask
names explicitly — is the gap.**

### Available today

| Need | Source | Note |
| --- | --- | --- |
| Stable learner id | `dim_user.user_global_id` | Keycloak `sub`; already the cross-system identifier. Survives email changes. |
| Email, full name | `dim_user.email`, `.full_name` | `email` is a *coalesce* across platform accounts — the most recently active address, not necessarily the one the org enrolled them under. Not a safe join key. |
| Org membership roster | `bridge_user_organization` | `(user_fk, organization_fk, userorganization_is_manager)`. Includes members with zero enrollments. |
| Org identity | `dim_organization` | `sso_organization_id` is the Keycloak org UUID the existing tenant already filters on. |
| Contract | `dim_contract` | Name, term, `b2b_contract_max_learners` (seat limit), membership type. |
| Contract → course run | `bridge_organization_courserun` | Grain `(org, contract, courserun)`. |
| Enrolled when | `tfact_enrollment.enrollment_created_on` | Plus `enrollment_is_active`, `enrollment_mode`, `enrollment_status`. |
| Completed | `tfact_grade` (`is_passing`, `grade_value`, `letter_grade`) + `tfact_certificate` (`certificate_issued_on`, `certificate_is_revoked`) | Two distinct signals — see below. |
| Course run metadata | `dim_course_run` | Title, start/end, `is_current`. |

### The gaps

**1. No learner-grain view exists.** All eight `b2b_analytics` MVs are
pre-aggregated by design. A learner roster needs a new dbt model — call it
`mv_b2b_learner_enrollment` at `(org × contract × courserun × learner)` — plus a
learner-grain rollup. This is the bulk of the upstream work, but it is
straightforward: the joins are exactly those in
`mv_b2b_enrollment_completion_funnel.sql` with the `group by` removed and
`dim_user` added.

**2. Activity is keyed on email, not `user_fk`.** Per-learner activity lives
only in `reporting.organization_administration_report`, whose grain is
`(courserun_readable_id, organization_key, user_email, activity_date)`. The
dimensional facts key on `user_fk`. Joining the report back to `dim_user` on
email will silently drop or mis-attribute a tail of learners, because
`dim_user.email` is itself a coalesced choice among a person's platform
accounts. Two ways out:

- add `user_fk` to `organization_administration_report`, or
- build the activity rollup directly from `tfact_video_events`,
  `tfact_problem_events`, `tfact_discussion_events`,
  `tfact_course_navigation_events` and `tfact_chatbot_events`, which already
  carry `user_fk`.

The second is more work but produces a correct `user_fk`-keyed
`(learner × courserun × day)` fact that the aggregate MVs would also benefit
from. Recommend the second.

Until one lands, `last_active_on`, `days_active` and the per-run activity
counters ship as `null`. The spec marks them `x-data-readiness: pending-model`
so a partner sizing the integration knows which columns to expect empty.

**3. `organization_key` is unreliable for activity attribution.** In
`organization_administration_report` it is
`COALESCE(b2b_contract_to_courseruns.organization_key, user_course_roles.organization)`
— the fallback is free text. The existing MVs drop rows where it is null. A
learner-grain view must attribute through the contract, never the free-text
fallback, or a partner will see learners who are not theirs.

**4. Roster and enrollment disagree, and that is signal.** A learner can be on
`bridge_user_organization` with no enrollment (assigned, unstarted seat) or
enrolled under a contract without a roster row (provisioning lag). Rather than
picking one source and hiding the discrepancy, the spec surfaces
`membership_source: roster | enrollment | both`. The assigned-but-unstarted seat
is normally the most actionable row on a training dashboard, and it is invisible
in every existing MV.

**5. Passing ≠ certified.** `tfact_grade.is_passing` and an unrevoked
`tfact_certificate` are separate facts that legitimately disagree — certificates
are issued on a schedule after grading, and audit-mode enrollments never
certify. Exposing both raw invites partners to build their own inconsistent
derivation, so the spec adds a derived `completion_status` enum
(`not_started | in_progress | passed | certified`) as the single field to read,
with the raw signals alongside.

**6. Learner consent does not exist yet, and it gates the outcome columns.** The
only consent-shaped fields in the warehouse today are email-marketing opt-in
(`irx__*__email_opt_in`), Emeritus/Global Alumni GDPR consent dates, and
Keycloak's OAuth client-consent representations. None of them is a
learner-to-organization data-sharing consent. It needs to resolve per
`(user_fk, organization_fk)` — either a column on `bridge_user_organization` or
a fact keyed the same way — so the learner-grain models can join it directly.
See §4.

**7. MITx Online only.** Every `b2b_analytics` MV filters
`org.platform = 'mitxonline'`. xPro B2B is order-based
(`int__mitxpro__b2becommerce_b2border`) and has no contract→courserun bridge.
Out of scope; say so to partners rather than letting them discover it.

**8. Freshness.** The MVs are `refresh_method='manual'`, driven by a Dagster
asset. "Last active" is as stale as the last refresh. The `as_of` envelope the
existing tenant already returns carries this honestly and doubles as the
`updated_since` cursor for incremental sync.

## 2. Existing tenant, or a new one?

**A new tenant.** Not a close call.

### It has the opposite privacy posture

`b2b_dashboard` is defined by what it does not emit. Its description string is
"Aggregated-only… No individual learner PII", and `core/anonymization.py`
enforces a k-anonymity floor of 5 on every row, with `CohortPolicy.__post_init__`
raising at import time if a derived column names an unfloored cohort. That
machinery, and the tests around it, exist to make "this tenant cannot disclose an
individual" a property you can check by reading one file.

A learner roster is individually identifying by construction. Putting it behind
the same mount means either exempting one router from `suppress_small_cohorts` —
which makes the invariant false while leaving the code that asserts it in place —
or applying the floor and shipping an endpoint that suppresses every row it
exists to return.

### Its auth model cannot run in an M2M flow

`require_org_manager` needs three things a client-credentials token does not
have: an `organization` claim to check membership against, a `sub` to name in
the MITx Online round-trip, and a human whose `is_manager` flag was curated in
Django admin. There is no user in this flow. The org grant has to come from the
*client's* registration, which is a different check, reached by a different
code path, with different failure modes.

### The blast radius attaches to a different principal

Credential issuance and revocation, rate limits, audit logging, and the DPA that
permits learner-level disclosure all attach to the partner client, not to a
logged-in org manager. Those are exactly the knobs the architecture already puts
in each tenant's own `config.py` and `auth.py`.

### It costs one package and one registry line

`main.py`'s `TENANTS` list is the documented extension point. A new tenant gets
its own OpenAPI document at its own mount — which is precisely the artifact you
want to hand to a partner, without the aggregate dashboard's endpoints in it.

It also positions the pending tenant-isolation work correctly: the README already
flags that per-tenant Vault role / StarRocks user with schema-scoped grants is
outstanding. The PII-bearing tenant is the one that most needs it, and separating
now means the learner-grain schema can be granted to that role alone later.

### Proposed shape

```
src/ol_analytics_api/tenants/b2b_learner_records/
  app.py       # create_app(); title/description say "identifiable learner records"
  config.py    # own StarRocks schema, own page caps, own audit settings
  auth.py      # client-credentials principal, org-grant check, scope gating
  models.py    # Learner, Enrollment, CourseRun — no CohortPolicy
  routers/
    organizations.py
```

Mounted at `/api/v1/learner-records`. Reuses `core/db/*`, `core/health.py`,
`core/errors.py`, `core/observability/*` unchanged. Deliberately does **not**
import `core/anonymization.py` — a reader should be able to tell the two tenants'
postures apart from the import list.

Naming it `b2b_learner_records` rather than `b2b_dashboard_v2` or similar keeps
the distinction legible at the mount path, in log lines, and in the readiness
sub-path.

## 3. Scope

### In

Three collections, all org-scoped, all read-only:

- `GET /organizations/{organization_id}/learners` — learner grain, roster plus
  progress rollup. Answers "who is on our licence and how are they doing".
- `GET /organizations/{organization_id}/enrollments` — `(learner × contract ×
  course run)` grain, matching `mv_b2b_learner_enrollment` above. Answers "did
  this learner complete this course". The primary endpoint;
  `/learners` is a convenience rollup over the same records.
- `GET /organizations/{organization_id}/courses` — the contracts and course runs
  the identifiers refer to. Small, slow-changing, no personal data, cacheable.

Plus `updated_since` incremental sync on the two record collections, because a
partner mirroring into their own LMS should not re-read the whole licence daily.

**Two delivery channels over one schema.** The REST API above, and a
per-organization bulk export (S3/SFTP) written on each refresh, discoverable via
`/organizations/{id}/exports`. The export is a second encoding of the same
records under the same field names, produced by the same query with the same
consent enforcement — not a second data product with its own semantics. A
partner doing full reloads into an internal LMS should use the export; a partner
rendering a dashboard should use the API.

### Out (for now)

- **Program-grain progress.** `tfact_enrollment.enrollment_scope='program'` and
  `tfact_certificate.certificate_scope='program'` exist, so this is additive
  later. Left out of v1 to keep the first contract small.
- **Per-block / per-assessment detail.** `afact_problem_engagement` and friends
  are per-block, and exposing them is a different product (an LRS feed, plausibly
  xAPI/Caliper) with a much larger surface.
- **Write operations.** Seat assignment and enrollment stay in MITx Online.
- **xPro B2B.** No contract→courserun mapping exists.
- **Learner-level data for the interactive MIT Learn dashboard.** An org manager
  arguably has the same entitlement, but that is a user-authenticated consumer
  with its own governance question. If it is wanted later, `b2b_dashboard` can
  add a router over the same StarRocks views under its own gate. Keeping the
  concerns separate now is cheaper than un-merging them later.

### Identity is not redacted

Every record carries `email` and `full_name`, and there is one scope,
`learner-records:read`.

Redacting identity would protect nothing. The organization already holds its
learners' names and addresses: they are its employees or students, and it
assigned the seats. A contracted provider reads on the organization's behalf
under the contract and handles per-user access in its own LMS. Consent governs
outcomes (§4), not identity.

One read scope also means one bulk export per organization serves every client.


## 4. Learner consent

**Consent gates outcomes, not identity.** The organization already holds its
learners' names and addresses — it assigned the seats. What a learner opts into
sharing is their *course status*: completion, progress and activity. So consent
is enforced by suppressing outcome fields on a record that still appears, rather
than by excluding the record.

Concretely, every learner and enrollment record carries `outcomes_shared`. When
it is false, every progress, completion and activity field on that record is
null; identity, contract, course run and enrollment facts are unaffected. The
envelope's `outcomes_withheld_count` reports how many records in the result are
in that state.

The field does not exist upstream yet. Until it ships, the tenant's
`consent_fail_open` setting (`OL_ANALYTICS_API_B2B_LEARNER_RECORDS_CONSENT_FAIL_OPEN`)
decides every record. It defaults to false, which fails closed: every record
reads `outcomes_shared: false` and the outcome columns are uniformly null. That
is a degraded response rather than an empty one, which means partner
integration can proceed against real records. A deployment that sets it to
true discloses outcomes for learners with no recorded decision. Once the field
lands, a recorded decision always wins and the setting covers only learners
with none.

### Why suppression rather than exclusion

Excluding non-consenting learners entirely was the obvious first design, and it
is worse on every axis that matters here:

* **Seat accounting breaks.** A 50-seat licence would read as 12 learners, and
  the organization's own roster would not reconcile against the API.
* **It protects nothing extra.** The organization holds the roster and the
  identities already. Any exclusion is trivially reversible by set difference.
* **It complicates sync.** Under exclusion, a withdrawal removes a row, and
  absence is not a signal an incremental consumer can act on — so withdrawals
  would need a separate tombstone record type to propagate at all. Under
  suppression a withdrawal is simply a changed record with `outcomes_shared:
  false` and nulled outcomes; a client that upserts normally drops the data it
  held, with no extra object type and no retention window to reason about.

That third point is the one that would have been expensive to discover late.

### What the upstream field must provide

1. **Withdrawal must be a retained state change, not a deleted row.** A deleted
   consent row is indistinguishable from one never granted, and the record's
   `updated_since` timestamp would not move — so the withdrawal never reaches
   the partner holding a copy. Model it as a status plus a change timestamp on a
   persistent row.
2. **Resolvable per `(learner, organization)`.** A learner holding seats under
   two organizations should be able to share with one and not the other. A
   global flag works mechanically but makes withdrawal all-or-nothing.
3. **A change timestamp**, since `updated_since` sync is driven off it.
4. **Three states distinguishable upstream** — `never_asked`, `declined`,
   `withdrawn` — even though all three render as `outcomes_shared: false`. The
   organization needs the breakdown to run its own opt-in campaign.
5. **It has to reach the warehouse**, as a column on `bridge_user_organization`
   or a fact keyed on `(user_fk, organization_fk)`. The API cannot consult MITx
   Online per row.

### Settled: aggregates are exempt

The existing `b2b_dashboard` tenant's k-anonymized org-level views disclose no
individual and continue to cover the whole cohort. No consent join, no change to
`mv_b2b_*`.

### Settled: the organization can see who declined

The organization holds the full roster and the identities, so it can see exactly
which of its learners carry `outcomes_shared: false`. Field suppression conceals
a learner's *outcomes*, not their *decision*, and no response shape changes that
while records stay individually identifiable.

This service doesn't try to. How an organization may use learner data is set by
its contract, which legal and contracting own. Issuing a client presumes those
terms are already in place. The record shape stays as specified: per-learner,
identity intact, outcomes nulled when `outcomes_shared` is false.

### Settled: a contracted provider receives identity

Yes. Identity is not redacted for any client (§3).

### Settled: who authorizes a provider, through what workflow?

The contract settles access. MIT issues one Keycloak client per contracted
integration, with the organizations it may read carried as a claim. The
partner handles per-user authorization in its own LMS. See
[`b2b-learner-records-provider-authorization.md`](b2b-learner-records-provider-authorization.md).
