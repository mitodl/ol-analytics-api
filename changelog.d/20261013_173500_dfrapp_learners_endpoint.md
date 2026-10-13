### Added

- `b2b_dashboard`: `GET /organizations/{org}/contracts/{id}/learners` lists learners paged by learner, each with all of their active enrollments, for the mit-learn progress grid. A learner is listed when one of their enrollments matches every filter (`search`, `completion_status`, `needs_attention`). `limit` and `offset` count learners, with a default and maximum of 100.
