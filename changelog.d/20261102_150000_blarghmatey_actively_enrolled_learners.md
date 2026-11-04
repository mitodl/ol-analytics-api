### Added

- `b2b_dashboard`: the organization and contract `/contract-utilization`, `/enrollment-funnel` and `/content-engagement` endpoints and `/admin/contract-health` return `actively_enrolled_learners`, the learners whose enrollment is still active, and `/enrollment-funnel` returns `actively_enrolled_rate_pct`. `active_learners` read as course activity, which is `monthly_active_learners`, so the enrollment measure gets its own name. Requires `mv_b2b_enrollment_completion_funnel`, `mv_b2b_contract_utilization`, `mv_b2b_mit_admin_contract_health`, `mv_b2b_content_engagement_depth` and `mv_b2b_contract_content_engagement_depth` built from ol-data-platform#3222 or later.

### Deprecated

- `b2b_dashboard`: `active_learners` on `/contract-utilization`, `/enrollment-funnel` and `/admin/contract-health`, `active_rate_pct` on `/enrollment-funnel`, and `total_enrolled_learners` on `/content-engagement`. Each always equals its replacement and is withheld with it; they will be removed once MIT Learn reads the new names.
