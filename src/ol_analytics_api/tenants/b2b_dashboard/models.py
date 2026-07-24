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
    """

    organization_id: str
    as_of: datetime.datetime | None
    data: list[RowT]


class AdminAnalyticsResponse[RowT: SQLModel](BaseModel):
    """Envelope for admin endpoints, which span all orgs — so no single
    ``organization_id`` applies (see Analytics API Endpoints epic)."""

    as_of: datetime.datetime | None
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
    contract_pk: int
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
    contract_pk: int
    b2b_contract_name: str
    courserun_pk: int
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

    The activity totals are summed over ``monthly_active_learners`` (the
    primary cohort, already floored), so they carry no sub-cohort of their
    own to suppress. Only the distinct-learner counts do.
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
    contract_pk: int
    b2b_contract_name: str
    program_pk: int
    program_title: str
    total_courses: int
    enrolled_in_contract_courses: int
    enrolled_via_program: int | None
    program_course_completers: int | None


class ContentEngagementDepth(SQLModel):
    """mv_b2b_content_engagement_depth — grain: org x course_run (all-time).

    The video/problem totals and their averages are attributable to
    ``engaged_learners`` (the totals are summed over exactly that group, the
    averages divide by it), so both are suppressed when that count is
    sub-floor; the chatbot totals likewise track ``chatbot_users``.
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
    contract_pk: int
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
