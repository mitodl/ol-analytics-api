### Added

- `b2b_dashboard`: `/engagement-trend` and `/contracts/{contract_id}/engagement-trend` return `newly_certified_learners`, the learners who earned a certificate in the month. Every other dashboard endpoint uses `certified_learners` for learners holding an unrevoked certificate, so the trend count gets its own name. Requires `mv_b2b_monthly_engagement_trend` and `mv_b2b_contract_monthly_engagement_trend` built from ol-data-platform#3076 or later.

### Deprecated

- `b2b_dashboard`: `certified_learners` on `/engagement-trend` and `/contracts/{contract_id}/engagement-trend`. It always equals `newly_certified_learners` and is withheld with it; it will be removed once MIT Learn reads `newly_certified_learners`.
