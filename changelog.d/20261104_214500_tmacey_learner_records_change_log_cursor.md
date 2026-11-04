### Changed

- `b2b_learner_records`: Breaking: `/learners` and `/enrollments` return removed records under `updated_since`, so `data` items are a `Learner` or `RemovedLearner` (an `Enrollment` or `RemovedEnrollment`), told apart by `removed`. A removed record carries only its identity fields and `removed: true`; a client drops the record it names. Full records gain `removed: false`. A client generated from the previous spec rejects the removed shape.
- `b2b_learner_records`: `updated_since` and `updated_before` compare each record's change time from the MV change logs (`mv_b2b_learner_changes`, `mv_b2b_learner_enrollment_changes`) instead of `record_updated_on`, so any change to a record's fields returns it, activity and `content_progress` included. Requires the change logs from ol-data-platform#2862 in the target environment; the first incremental read after they are created returns every record once.
- `b2b_learner_records`: an `updated_since` more than 90 days old is a 400 (`invalid_parameter`). Removed records are kept for 90 days, so an older cursor could miss one; the client reloads in full.

### Removed

- `b2b_learner_records` contract (`docs/openapi/b2b-learner-records-v1.yaml`): `Learner.is_current`, which was never implemented. A learner no longer in the organization arrives as a `RemovedLearner`.
