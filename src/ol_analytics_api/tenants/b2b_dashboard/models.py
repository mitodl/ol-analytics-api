"""Response schemas mirroring the 6 StarRocks B2B analytics materialized views.

Column sets match the dbt models in `ol-data-platform`'s
`models/b2b_analytics/*.sql` (mitodl/ol-data-platform PR #2329) exactly.
These are plain SQLModel (Pydantic) schemas, not ORM tables — StarRocks-side
schema is owned by dbt, not by this service.

Every row model declares a ``cohort_policy`` (see core.anonymization): the
distinct-entity counts subject to the k-anonymity floor and the derived
values computed over them. The response layer nulls sub-floor secondary
counts and their derivatives, so any count/rate/average column that can be
suppressed is typed Optional even though the view never emits a NULL there.
"""

from __future__ import annotations

import datetime
from typing import ClassVar

from pydantic import BaseModel
from sqlmodel import SQLModel

from ol_analytics_api.core.anonymization import CohortPolicy


class OrgAnalyticsResponse[RowT: SQLModel](BaseModel):
    """Envelope for every org-scoped endpoint.

    ``as_of`` is the last MV-refresh time (None until the first refresh
    finishes); the dashboard displays it so a manager knows how fresh the
    numbers are. ``data`` is post-suppression — a manager-authorized org
    with no (or only sub-floor) rows returns ``data: []``, not a 404.

    ``total_count`` is how many rows this org has in the backing view *after*
    the anonymization floor, i.e. across every page. Without it a client
    cannot distinguish "this org has 200 course runs" from "this org has more
    than the page cap and the rest were silently dropped", so a truncated
    dashboard would look complete. Compare it against ``len(data)`` plus the
    request's ``offset`` to decide whether to page further.
    """

    organization_id: str
    as_of: datetime.datetime | None
    total_count: int
    data: list[RowT]


class AdminAnalyticsResponse[RowT: SQLModel](BaseModel):
    """Envelope for admin endpoints, which span all orgs — so no single
    ``organization_id`` applies (see Analytics API Endpoints epic).

    ``total_count`` carries the same meaning as on ``OrgAnalyticsResponse``,
    over all orgs rather than one."""

    as_of: datetime.datetime | None
    total_count: int
    data: list[RowT]


class ContractUtilization(SQLModel):
    """mv_b2b_contract_utilization — grain: org x contract."""

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="seats_consumed",
        secondary=("active_learners", "learners_certified"),
        derived={"completion_rate_pct": ("learners_certified",)},
    )

    organization_key: str
    organization_name: str
    contract_pk: str
    b2b_contract_name: str
    b2b_contract_is_active: bool
    b2b_contract_start_date: datetime.date | None
    b2b_contract_end_date: datetime.date | None
    seat_limit: int | None
    b2b_contract_membership_type: str | None
    seats_consumed: int
    active_learners: int | None
    learners_certified: int | None
    seat_utilization_pct: float | None
    completion_rate_pct: float | None


class EnrollmentCompletionFunnel(SQLModel):
    """mv_b2b_enrollment_completion_funnel — grain: org x contract x course_run."""

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="enrolled_learners",
        secondary=("active_learners", "passing_learners", "certified_learners"),
        derived={
            "active_rate_pct": ("active_learners",),
            "completion_rate_pct": ("certified_learners",),
        },
    )

    organization_key: str
    organization_name: str
    contract_pk: str
    b2b_contract_name: str
    courserun_pk: str
    courserun_readable_id: str
    courserun_title: str
    enrolled_learners: int
    active_learners: int | None
    passing_learners: int | None
    certified_learners: int | None
    active_rate_pct: float | None
    completion_rate_pct: float | None


class MonthlyEngagementTrend(SQLModel):
    """mv_b2b_monthly_engagement_trend — grain: org x year_month.

    KNOWN SUPPRESSION GAP (verified against the dbt SQL 2026-07-31). The
    activity totals are *not* attributable to ``monthly_active_learners``.
    Each is a plain SUM over the source report, so only the learners who did
    that specific thing contribute: ``total_videos_watched`` is really summed
    over the video-watcher cohort, ``total_problems_attempted`` over the
    problem-attempter cohort, ``total_chatbot_interactions`` over the
    chatbot-user cohort. Because ``active_count`` is set by *any* activity
    (organization_administration_report.sql:301-307), each of those cohorts is
    a strict subset of ``monthly_active_learners`` — so clearing the primary
    floor does not imply they cleared it. A month with 40 active learners can
    carry a chatbot total contributed by exactly one of them.

    None of those three cohorts is emitted as a column by
    mv_b2b_monthly_engagement_trend.sql, so this model cannot floor them: a
    ``derived`` entry needs a cohort count present in the row. Closing the gap
    requires the MV to publish them (see the ol-data-platform follow-up task);
    mapping the totals to the primary would be a no-op, since rows below the
    primary floor are dropped outright and the primary is never in the
    suppressed set.

    ``new_enrollments`` and ``certificates_earned`` are likewise SUMs of
    per-learner-per-day markers rather than distinct-learner counts, so they
    count *events*: one learner enrolling in six courses reads as
    ``new_enrollments == 6`` and clears a floor of 5 on its own. They are kept
    as ``secondary`` because flooring an event count is still strictly better
    than not flooring it, but it is a weaker guarantee than the name implies.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="monthly_active_learners",
        secondary=("new_enrollments", "certificates_earned"),
    )

    organization_key: str
    organization_name: str
    activity_year_and_month: str
    monthly_active_learners: int
    new_enrollments: int | None
    certificates_earned: int | None
    total_videos_watched: int
    total_problems_attempted: int
    total_chatbot_interactions: int


class ProgramFunnel(SQLModel):
    """mv_b2b_program_funnel — grain: org x contract x program.

    ``total_courses`` counts courses, not learners, so it is not a cohort.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="enrolled_in_contract_courses",
        secondary=("enrolled_via_program", "program_course_completers"),
    )

    organization_key: str
    organization_name: str
    contract_pk: str
    b2b_contract_name: str
    program_pk: str
    program_title: str
    total_courses: int
    enrolled_in_contract_courses: int
    enrolled_via_program: int | None
    program_course_completers: int | None


class ContentEngagementDepth(SQLModel):
    """mv_b2b_content_engagement_depth — grain: org x course_run (all-time).

    The chatbot columns are exact: ``total_chatbot_interactions`` sums over,
    and ``chatbot_adoption_pct`` divides by, ``chatbot_users`` — which this
    view does emit, so both are correctly floored. ``engagement_rate_pct`` is
    ``engaged_learners / total_enrolled_learners``, also correct.

    KNOWN SUPPRESSION GAP (verified against the dbt SQL 2026-07-31). The
    video and problem columns are *not* computed over ``engaged_learners``,
    despite the ``_per_engaged_learner`` naming. In
    mv_b2b_content_engagement_depth.sql:24-34 the averages divide by
    ``count(distinct case when videos_watched > 0 ...)`` and
    ``count(distinct case when problems_count > 0 ...)`` respectively — the
    video-watcher and problem-attempter cohorts, neither of which this view
    emits. Since ``active_count`` is set by *any* activity
    (organization_administration_report.sql:301-307), both are strict subsets
    of ``engaged_learners``, so a row with 30 engaged learners may still carry
    an average taken over a single video-watcher — precisely the "an average
    over one learner *is* that learner's value" disclosure that
    core.anonymization exists to prevent.

    Mapping these to ``engaged_learners`` is therefore necessary but not
    sufficient: it correctly suppresses when the superset is sub-floor, and
    never over-suppresses, but it cannot catch a sub-floor subset. The mapping
    is kept for the protection it does give. Closing the gap needs a dbt
    change — either divide by ``engaged_learners`` so the column matches its
    name, or emit the two cohort counts so they can be floored and mapped.
    See the ol-data-platform follow-up task.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=("engaged_learners", "chatbot_users", "certificates_earned"),
        derived={
            "engagement_rate_pct": ("engaged_learners",),
            "total_videos_watched": ("engaged_learners",),
            "avg_videos_per_engaged_learner": ("engaged_learners",),
            "total_problems_attempted": ("engaged_learners",),
            "avg_problems_per_engaged_learner": ("engaged_learners",),
            "total_chatbot_interactions": ("chatbot_users",),
            "chatbot_adoption_pct": ("chatbot_users",),
        },
    )

    organization_key: str
    organization_name: str
    courserun_readable_id: str
    courserun_title: str
    total_enrolled_learners: int
    engaged_learners: int | None
    engagement_rate_pct: float | None
    total_videos_watched: int | None
    avg_videos_per_engaged_learner: float | None
    total_problems_attempted: int | None
    avg_problems_per_engaged_learner: float | None
    total_chatbot_interactions: int | None
    chatbot_users: int | None
    chatbot_adoption_pct: float | None
    certificates_earned: int | None


class MitAdminContractHealth(SQLModel):
    """mv_b2b_mit_admin_contract_health — grain: org x contract (MIT admin only)."""

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="seats_consumed",
        secondary=("active_learners", "certified_learners"),
        derived={"completion_rate_pct": ("certified_learners",)},
    )

    organization_key: str
    organization_name: str
    contract_pk: str
    b2b_contract_name: str
    b2b_contract_is_active: bool
    b2b_contract_start_date: datetime.date | None
    b2b_contract_end_date: datetime.date | None
    seat_limit: int | None
    b2b_contract_membership_type: str | None
    seats_consumed: int
    active_learners: int | None
    certified_learners: int | None
    seat_utilization_pct: float | None
    completion_rate_pct: float | None
    health_status: str
