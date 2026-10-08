### Changed

- `b2b_dashboard`: engagement-trend rows are withheld on `contributing_learners` (learners who were active, enrolled or were certified in the month) instead of `monthly_active_learners`. A month with enough certified or enrolling learners and too few active ones is returned with `monthly_active_learners` set to `null`. Both engagement-trend endpoints need the views from [ol-data-platform #2881](https://github.com/mitodl/ol-data-platform/pull/2881) ([#95](https://github.com/mitodl/ol-analytics-api/pull/95))

### Fixed

- `b2b_dashboard`: content-engagement withholds `certificates_earned` when the enrolled learners without a certificate are fewer than the floor ([#95](https://github.com/mitodl/ol-analytics-api/pull/95))
- `b2b_dashboard`: the organization engagement trend withholds `monthly_active_learners` for a month in which a contract's count is withheld, so it cannot be recovered by subtracting the visible contracts ([#95](https://github.com/mitodl/ol-analytics-api/pull/95))
