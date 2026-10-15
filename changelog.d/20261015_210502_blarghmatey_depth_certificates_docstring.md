### Fixed

- The `ContentEngagementDepth` schema description no longer says `certificates_earned` is a subset of `total_enrolled_learners` or that a run offered under two contracts double-counts it. Since ol-data-platform#2913 it counts certificate holders whatever their enrollment status, and since ol-data-platform#2982 each learner is attributed to one contract. It describes what the containment declaration from #95 withholds and publishes. Responses are unchanged.
