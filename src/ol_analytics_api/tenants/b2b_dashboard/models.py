"""Response schemas mirroring the 6 StarRocks B2B analytics materialized views.

Column sets match the dbt models in `ol-data-platform`'s
`models/b2b_analytics/*.sql` (mitodl/ol-data-platform PR #2329) exactly.
These are plain SQLModel (Pydantic) schemas, not ORM tables — StarRocks-side
schema is owned by dbt, not by this service.
"""

from __future__ import annotations

import datetime

from sqlmodel import SQLModel


class ContractUtilization(SQLModel):
    """mv_b2b_contract_utilization — grain: org x contract."""

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
    active_learners: int
    learners_certified: int
    seat_utilization_pct: float | None
    completion_rate_pct: float | None


class EnrollmentCompletionFunnel(SQLModel):
    """mv_b2b_enrollment_completion_funnel — grain: org x contract x course_run."""

    organization_key: str
    organization_name: str
    contract_pk: int
    b2b_contract_name: str
    courserun_pk: int
    courserun_readable_id: str
    courserun_title: str
    enrolled_learners: int
    active_learners: int
    passing_learners: int
    certified_learners: int
    active_rate_pct: float | None
    completion_rate_pct: float | None


class MonthlyEngagementTrend(SQLModel):
    """mv_b2b_monthly_engagement_trend — grain: org x year_month."""

    organization_key: str
    organization_name: str
    activity_year_and_month: str
    monthly_active_learners: int
    new_enrollments: int
    certificates_earned: int
    total_videos_watched: int
    total_problems_attempted: int
    total_chatbot_interactions: int


class ProgramFunnel(SQLModel):
    """mv_b2b_program_funnel — grain: org x contract x program."""

    organization_key: str
    organization_name: str
    contract_pk: int
    b2b_contract_name: str
    program_pk: int
    program_title: str
    total_courses: int
    enrolled_in_contract_courses: int
    enrolled_via_program: int
    program_course_completers: int


class ContentEngagementDepth(SQLModel):
    """mv_b2b_content_engagement_depth — grain: org x course_run (all-time)."""

    organization_key: str
    organization_name: str
    courserun_readable_id: str
    courserun_title: str
    total_enrolled_learners: int
    engaged_learners: int
    engagement_rate_pct: float | None
    total_videos_watched: int
    avg_videos_per_engaged_learner: float | None
    total_problems_attempted: int
    avg_problems_per_engaged_learner: float | None
    total_chatbot_interactions: int
    chatbot_users: int
    chatbot_adoption_pct: float | None
    certificates_earned: int


class MitAdminContractHealth(SQLModel):
    """mv_b2b_mit_admin_contract_health — grain: org x contract (MIT admin only)."""

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
    active_learners: int
    certified_learners: int
    seat_utilization_pct: float | None
    completion_rate_pct: float | None
    health_status: str
