### Changed

- `b2b_dashboard` and `b2b_learner_records` read `completion_status`, `needs_attention_since`, `is_certified` and `is_in_progress` from `mv_b2b_learner_enrollment` instead of deriving them in SQL here. The rules are defined once in ol-data-platform's `afact_learner_courserun_progress` (ol-data-platform#2855, #2904). Needs attention now compares `needs_attention_since` with the cluster's UTC date. Responses are unchanged. Requires a `mv_b2b_learner_enrollment` built from ol-data-platform#2904 or later.
