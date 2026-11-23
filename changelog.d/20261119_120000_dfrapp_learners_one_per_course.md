### Added

- `b2b_dashboard`: `/courses` lists a contract's courses in the learner dashboard's order, the courses with a run on the contract that are in one of its programs, for the Learners page's columns and module filter. `/learners` accepts a repeatable `course_readable_id` filter, and each of its `enrollments` carries `course_readable_id` and `course_title`. Requires `course_readable_id` and `course_title` on `mv_b2b_contract_courserun` and `mv_b2b_contract_member_enrollment` from ol-data-platform#3425 in the target environment; until both views are rebuilt with them, `/learners` and `/courses` fail.

### Changed

- `b2b_dashboard`: `/learners` returns one entry per course in `/courses` instead of one per course run, so the Learners page agrees with the learner's own dashboard (mitodl/hq#14464). When a learner has more than one run of a course, the entry is the run the dashboard shows: a certified run, else the run open now, else the next to start, else the one that started most recently. Its status, progress, last activity and needs attention, and every filter, read that run. `courserun_readable_id` matches the run an entry shows. A run of a course `/courses` doesn't list has no entry, though its learner is still listed. `not_enrolled` counts missing courses rather than missing runs. `/course-runs` and `/learner-progress` don't change.
