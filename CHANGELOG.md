# Changelog

Entries are collected from the fragments in `changelog.d/` by
[scriv](https://scriv.readthedocs.io/) when a release is cut. Entries before
the switch to scriv were reconstructed from the git history.

<!-- scriv-insert-here -->

<a id='changelog-2026.10.20.1'></a>
## 2026.10.20.1 (2026-10-20)

### Added

- `b2b_dashboard`: `/contract-utilization` returns `certified_learners`, the name every other dashboard endpoint uses for learners holding an unrevoked certificate. Requires a `mv_b2b_contract_utilization` built from ol-data-platform#3011 or later.

### Changed

- `b2b_dashboard`: engagement-trend rows are withheld on `contributing_learners` (learners who were active, enrolled or were certified in the month) instead of `monthly_active_learners`. A month with enough certified or enrolling learners and too few active ones is returned with `monthly_active_learners` set to `null`. Both engagement-trend endpoints need the views from [ol-data-platform #2881](https://github.com/mitodl/ol-data-platform/pull/2881) ([#95](https://github.com/mitodl/ol-analytics-api/pull/95))

- `b2b_dashboard` and `b2b_learner_records` read `completion_status`, `needs_attention_since`, `is_certified` and `is_in_progress` from `mv_b2b_learner_enrollment` instead of deriving them in SQL here. The rules are defined once in ol-data-platform's `afact_learner_courserun_progress` (ol-data-platform#2855, #2904). Needs attention now compares `needs_attention_since` with the cluster's UTC date. Responses are unchanged. Requires a `mv_b2b_learner_enrollment` built from ol-data-platform#2904 or later.

### Deprecated

- `b2b_dashboard`: `learners_certified` on `/contract-utilization`. It always equals `certified_learners` and is withheld with it; it will be removed once MIT Learn reads `certified_learners`.

### Fixed

- `b2b_dashboard`: content-engagement withholds `certificates_earned` when the enrolled learners without a certificate are fewer than the floor ([#95](https://github.com/mitodl/ol-analytics-api/pull/95))
- `b2b_dashboard`: the organization engagement trend withholds `monthly_active_learners` for a month in which a contract's count is withheld, so it cannot be recovered by subtracting the visible contracts ([#95](https://github.com/mitodl/ol-analytics-api/pull/95))

- `b2b_dashboard`: the `ContentEngagementDepth` schema description no longer says `certificates_earned` is a subset of `total_enrolled_learners` or that a run offered under two contracts double-counts it. Since [ol-data-platform #2913](https://github.com/mitodl/ol-data-platform/pull/2913) it counts certificate holders whatever their enrollment status, and since [ol-data-platform #2982](https://github.com/mitodl/ol-data-platform/pull/2982) each learner is attributed to one contract. It now describes what the containment declaration from [#95](https://github.com/mitodl/ol-analytics-api/pull/95) withholds and publishes. Responses are unchanged.

<a id='changelog-2026.10.14.1'></a>
## 2026.10.14.1 (2026-10-14)

### Added

- `b2b_dashboard`: `GET /organizations/{org}/contracts/{id}/learners` lists learners paged by learner, each with all of their active enrollments, for the mit-learn progress grid ([#107](https://github.com/mitodl/ol-analytics-api/pull/107)). A learner is listed when one of their enrollments matches every filter (`search`, `completion_status`, `needs_attention`). `limit` and `offset` count learners, with a default and maximum of 25, since each learner expands to one row per enrollment.

<a id='changelog-2026.10.8.1'></a>
## 2026.10.8.1 (2026-10-08)

### Added

- Changelog fragments with scriv. A change worth noting adds a fragment under `changelog.d/`, and the release job collects whatever fragments are present into `CHANGELOG.md` during the version bump.

<a id='changelog-2026.10.7.1'></a>
## 2026.10.7.1 (2026-10-07)

### Added

- `b2b_dashboard`: distinct-learner needs-attention count per contract, at `/organizations/{org}/needs-attention` and `/organizations/{org}/contracts/{id}/needs-attention` ([#89](https://github.com/mitodl/ol-analytics-api/pull/89))

### Changed

- CI runs the prek hooks through autofix.ci, with prek pinned in a `prek` dependency group ([#88](https://github.com/mitodl/ol-analytics-api/pull/88))
- Lockfile maintenance ([#90](https://github.com/mitodl/ol-analytics-api/pull/90))

### Fixed

- `b2b_dashboard`: `needs_attention` no longer flags learners who have passed or been certified ([#87](https://github.com/mitodl/ol-analytics-api/pull/87))

### Security

- `b2b_learner_records`: the tenant verifies the bearer JWT against the realm JWKS and no longer reads `X-Userinfo` ([#69](https://github.com/mitodl/ol-analytics-api/pull/69))

<a id='changelog-2026.10.1.1'></a>
## 2026.10.1.1 (2026-10-01)

### Added

- `b2b_dashboard`: `needs_attention` query param and per-row field on learner-progress ([#84](https://github.com/mitodl/ol-analytics-api/pull/84))

### Changed

- `astral-sh/setup-uv` action updated to v10.1.0 ([#76](https://github.com/mitodl/ol-analytics-api/pull/76))

<a id='changelog-2026.9.30.1'></a>
## 2026.9.30.1 (2026-09-30)

### Added

- `b2b_learner_records`: serve `/courses` from `mv_b2b_contract_courserun` ([#60](https://github.com/mitodl/ol-analytics-api/pull/60))
- `b2b_learner_records`: serve activity from the learner-records materialized views ([#62](https://github.com/mitodl/ol-analytics-api/pull/62))
- `b2b_learner_records`: bounded date-range filters for partitioned backfills ([#72](https://github.com/mitodl/ol-analytics-api/pull/72))
- `b2b_dashboard`: per-status counts on the learner-progress envelope ([#68](https://github.com/mitodl/ol-analytics-api/pull/68))
- `b2b_dashboard`: learner-progress serves activity and carries a needs-attention count ([#78](https://github.com/mitodl/ol-analytics-api/pull/78))
- `b2b_dashboard`: filter learner-progress by course run ([#79](https://github.com/mitodl/ol-analytics-api/pull/79))
- A per-tenant OpenAPI spec committed under `openapi/specs/`, regenerated by `bin/generate-openapi-spec` and diffed in CI ([#70](https://github.com/mitodl/ol-analytics-api/pull/70))

### Changed

- Dependency and lockfile updates ([#75](https://github.com/mitodl/ol-analytics-api/pull/75), [#77](https://github.com/mitodl/ol-analytics-api/pull/77))

### Fixed

- `b2b`: blank learner names are returned as `null` ([#71](https://github.com/mitodl/ol-analytics-api/pull/71))

<a id='changelog-2026.9.17.1'></a>
## 2026.9.17.1 (2026-09-17)

### Added

- `b2b_learner_records` tenant, a machine-to-machine per-learner progress API ([#56](https://github.com/mitodl/ol-analytics-api/pull/56))
- `b2b_dashboard`: contract-scoped learner-progress endpoint ([#59](https://github.com/mitodl/ol-analytics-api/pull/59))
- Descriptions on analytics response fields ([#57](https://github.com/mitodl/ol-analytics-api/pull/57))
- B2B learner records spec and provider-access decision record ([#55](https://github.com/mitodl/ol-analytics-api/pull/55))

### Changed

- Python base image updated to 3.14 ([#48](https://github.com/mitodl/ol-analytics-api/pull/48))
- Dependencies pinned and updated ([#46](https://github.com/mitodl/ol-analytics-api/pull/46), [#47](https://github.com/mitodl/ol-analytics-api/pull/47), [#49](https://github.com/mitodl/ol-analytics-api/pull/49), [#50](https://github.com/mitodl/ol-analytics-api/pull/50), [#51](https://github.com/mitodl/ol-analytics-api/pull/51), [#61](https://github.com/mitodl/ol-analytics-api/pull/61))

### Security

- Sentry events cap request bodies at 1KB and scrub Postgres `DETAIL` rows ([#54](https://github.com/mitodl/ol-analytics-api/pull/54))

<a id='changelog-2026.9.3.1'></a>
## 2026.9.3.1 (2026-09-03)

### Changed

- Renovate configuration ([#44](https://github.com/mitodl/ol-analytics-api/pull/44))

### Fixed

- OpenTelemetry instruments the FastAPI instance rather than the module attribute ([#52](https://github.com/mitodl/ol-analytics-api/pull/52))

<a id='changelog-2026.8.28.2'></a>
## 2026.8.28.2 (2026-08-28)

### Fixed

- `b2b_dashboard`: `contract_id` is typed as an integer ([#39](https://github.com/mitodl/ol-analytics-api/pull/39))

<a id='changelog-2026.8.28.1'></a>
## 2026.8.28.1 (2026-08-28)

### Added

- `b2b_dashboard`: contract-scoped endpoints mirroring mitxonline's manager dashboard ([#34](https://github.com/mitodl/ol-analytics-api/pull/34))

### Changed

- The reference Kubernetes manifest mirrors the deployed pod hardening ([#32](https://github.com/mitodl/ol-analytics-api/pull/32))

### Fixed

- Stop overriding the OpenTelemetry SDK's OTLP endpoint resolution ([#35](https://github.com/mitodl/ol-analytics-api/pull/35))

### Security

- `b2b_dashboard`: every activity aggregate is floored through the cohort that produced it ([#33](https://github.com/mitodl/ol-analytics-api/pull/33))

<a id='changelog-2026.8.12.1'></a>
## 2026.8.12.1 (2026-08-12)

### Fixed

- `b2b_dashboard`: contract, program, and course run primary key fields are typed as strings ([#29](https://github.com/mitodl/ol-analytics-api/pull/29))

<a id='changelog-2026.8.3.1'></a>
## 2026.8.3.1 (2026-08-03)

### Changed

- Granian is the application server ([#21](https://github.com/mitodl/ol-analytics-api/pull/21))

### Fixed

- `b2b`: the org-manager check authenticates to MITx Online with OAuth2 client credentials ([#22](https://github.com/mitodl/ol-analytics-api/pull/22))
- `b2b`: two `cohort_policy` docstrings that overstated their safety guarantees ([#23](https://github.com/mitodl/ol-analytics-api/pull/23))

### Security

- zizmor analysis of GitHub Actions workflows, SHA-pinned actions, and a 7-day `exclude-newer` window on dependency resolution ([#24](https://github.com/mitodl/ol-analytics-api/pull/24))

<a id='changelog-2026.7.30.1'></a>
## 2026.7.30.1 (2026-07-30)

### Added

- `b2b`: total row count in the analytics envelopes ([#15](https://github.com/mitodl/ol-analytics-api/pull/15))

### Changed

- CI: a dropped `pull_request` run can be recovered without an empty commit ([#17](https://github.com/mitodl/ol-analytics-api/pull/17))

### Fixed

- `b2b`: org endpoints are keyed on the Keycloak org UUID (`sso_organization_id`) ([#13](https://github.com/mitodl/ol-analytics-api/pull/13))

<a id='changelog-2026.7.22.1'></a>
## 2026.7.22.1 (2026-07-22)

First release.

### Added

- Multi-tenant FastAPI service serving aggregated B2B analytics from StarRocks materialized views ([#1](https://github.com/mitodl/ol-analytics-api/pull/1))
- `as_of` + `data` envelope on analytics responses ([#7](https://github.com/mitodl/ol-analytics-api/pull/7))
- Test coverage at 100% with coverage reporting in CI ([#8](https://github.com/mitodl/ol-analytics-api/pull/8))
- `bump-my-version` configuration for the ol-concourse release resource

### Changed

- Endpoint table, tenant contract, and per-view `as_of` from the architecture review ([#6](https://github.com/mitodl/ol-analytics-api/pull/6))

### Fixed

- Tenant readiness is scoped so one tenant's upstream can't fail the whole pod ([#3](https://github.com/mitodl/ol-analytics-api/pull/3))

### Security

- The k-anonymity floor applies across every cohort column ([#5](https://github.com/mitodl/ol-analytics-api/pull/5))
- StarRocks queries are bounded by `LIMIT`/pagination and query and pool timeouts ([#4](https://github.com/mitodl/ol-analytics-api/pull/4))
