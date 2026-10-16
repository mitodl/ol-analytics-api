### Added

- `b2b_dashboard`: `/contract-utilization` returns `certified_learners`, the name every other dashboard endpoint uses for learners holding an unrevoked certificate. Requires a `mv_b2b_contract_utilization` built from ol-data-platform#3011 or later.

### Deprecated

- `b2b_dashboard`: `learners_certified` on `/contract-utilization`. It always equals `certified_learners` and is withheld with it; it will be removed once MIT Learn reads `certified_learners`.
