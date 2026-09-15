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
from sqlmodel import Field, SQLModel

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

    organization_key: str = Field(
        description="Internal surrogate key identifying the organization."
    )
    organization_name: str = Field(description="Display name of the organization.")
    contract_pk: str = Field(description="Surrogate primary key of the B2B contract.")
    contract_id: int = Field(
        description="Numeric identifier of the B2B contract in the source system."
    )
    b2b_contract_name: str = Field(description="Name of the B2B contract.")
    b2b_contract_is_active: bool = Field(description="Whether the contract is currently active.")
    b2b_contract_start_date: datetime.date | None = Field(
        description="Date the contract's coverage begins, or null if unset."
    )
    b2b_contract_end_date: datetime.date | None = Field(
        description="Date the contract's coverage ends, or null if the contract has no end date."
    )
    seat_limit: int | None = Field(
        description=(
            "Maximum number of seats the contract allows. Zero or null means unlimited, and "
            "seat_utilization_pct is null for both."
        )
    )
    b2b_contract_membership_type: str | None = Field(
        description="Membership type configured for the contract, or null if not set."
    )
    seats_consumed: int = Field(
        description=(
            "Distinct learners enrolled in any course run covered by the contract. The row's "
            "primary cohort: a row whose count is below the anonymization floor is withheld "
            "entirely."
        )
    )
    active_learners: int | None = Field(
        description=(
            "Distinct seats_consumed learners with a currently active enrollment. Nulled when "
            "nonzero but below the anonymization floor."
        )
    )
    learners_certified: int | None = Field(
        description=(
            "Distinct seats_consumed learners who earned a non-revoked certificate. Nulled when "
            "nonzero but below the anonymization floor."
        )
    )
    seat_utilization_pct: float | None = Field(
        description="seats_consumed as a percentage of seat_limit."
    )
    completion_rate_pct: float | None = Field(
        description=(
            "learners_certified as a percentage of seats_consumed. Nulled whenever "
            "learners_certified is suppressed."
        )
    )


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

    organization_key: str = Field(
        description="Internal surrogate key identifying the organization."
    )
    organization_name: str = Field(description="Display name of the organization.")
    contract_pk: str = Field(description="Surrogate primary key of the B2B contract.")
    contract_id: int = Field(
        description="Numeric identifier of the B2B contract in the source system."
    )
    b2b_contract_name: str = Field(description="Name of the B2B contract.")
    courserun_pk: str = Field(description="Surrogate primary key of the course run.")
    courserun_readable_id: str = Field(
        description="Human-readable identifier of the course run (course + run tag)."
    )
    courserun_title: str = Field(description="Title of the course run.")
    enrolled_learners: int = Field(
        description=(
            "Distinct learners enrolled in the course run. The row's primary cohort: a row "
            "whose count is below the anonymization floor is withheld entirely."
        )
    )
    active_learners: int | None = Field(
        description=(
            "Distinct enrolled_learners with a currently active enrollment. Nulled when nonzero "
            "but below the anonymization floor."
        )
    )
    passing_learners: int | None = Field(
        description=(
            "Distinct enrolled_learners with a passing grade. Nulled when nonzero but below the "
            "anonymization floor."
        )
    )
    certified_learners: int | None = Field(
        description=(
            "Distinct enrolled_learners who earned a non-revoked certificate. Nulled when "
            "nonzero but below the anonymization floor."
        )
    )
    active_rate_pct: float | None = Field(
        description=(
            "active_learners as a percentage of enrolled_learners. Nulled whenever "
            "active_learners is suppressed."
        )
    )
    completion_rate_pct: float | None = Field(
        description=(
            "certified_learners as a percentage of enrolled_learners. Nulled whenever "
            "certified_learners is suppressed."
        )
    )


class MonthlyEngagementTrend(SQLModel):
    """mv_b2b_monthly_engagement_trend — grain: org x year_month.

    Every aggregate here is floored through the cohort that contributes to it,
    which the view publishes alongside it (ol-data-platform PR #2520).

    None of them is attributable to ``monthly_active_learners``. Each is a
    plain SUM over the source report, so only the learners who did that
    specific thing contribute — and clearing the primary floor says nothing
    about whether that narrower cohort cleared it. A month with 40 active
    learners can carry a chatbot total contributed by exactly one of them,
    which is why each total is ``derived`` from its own cohort rather than
    from the primary.

    How each cohort relates to the primary differs, and neither case makes
    mapping to the primary safe:

    - ``certified_learners``, ``video_watchers``, ``problem_attempters`` and
      ``chatbot_users`` are strict *subsets*. ``active_count`` is 1 when any
      of navigation, discussion, videos, problems, chatbot or certificate
      activity is nonzero (organization_administration_report.sql), so each
      of those actions sets it.
    - ``enrolling_learners`` is *not* a subset. ``enrolled_count`` is absent
      from that expression, so enrolling alone never sets ``active_count``
      and a learner who only enrolled is counted here but not in the primary.
      The row gate is unaffected — a month whose primary is sub-floor is
      dropped whole, which over-suppresses a large enrollment cohort rather
      than disclosing one — but the subset reasoning does not apply, and
      ``new_enrollments`` is floored through ``enrolling_learners`` on its
      own terms.

    ``new_enrollments`` and ``certificates_earned`` are SUMs of
    per-learner-per-course-run markers, so they count *events*, not learners:
    one learner enrolling in six runs reads as ``new_enrollments == 6`` and
    would clear a floor of 5 on its own. Flooring them directly is therefore
    the wrong instrument — they are ``derived`` from ``enrolling_learners``
    and ``certified_learners``, the distinct-learner counts they are actually
    attributable to, which do carry the floor.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="monthly_active_learners",
        secondary=(
            "enrolling_learners",
            "certified_learners",
            "video_watchers",
            "problem_attempters",
            "chatbot_users",
        ),
        derived={
            "new_enrollments": ("enrolling_learners",),
            "certificates_earned": ("certified_learners",),
            "total_videos_watched": ("video_watchers",),
            "total_problems_attempted": ("problem_attempters",),
            "total_chatbot_interactions": ("chatbot_users",),
        },
    )

    organization_key: str = Field(
        description="Internal surrogate key identifying the organization."
    )
    organization_name: str = Field(description="Display name of the organization.")
    activity_year_and_month: str = Field(
        description="Calendar year and month the activity occurred in (e.g. '2026-08')."
    )
    monthly_active_learners: int = Field(
        description=(
            "Distinct learners with any activity in the month — navigation, discussion, video, "
            "problem, chatbot, or certificate activity; enrolling alone does not count. The "
            "row's primary cohort: a month whose count is below the anonymization floor is "
            "withheld entirely."
        )
    )
    new_enrollments: int | None = Field(
        description=(
            "Total enrollment events in the month, counted per learner per course run (one "
            "learner enrolling in six runs counts as six). Derived from enrolling_learners and "
            "nulled whenever it is suppressed."
        )
    )
    enrolling_learners: int | None = Field(
        description=(
            "Distinct learners who enrolled in at least one course run during the month. Not a "
            "subset of monthly_active_learners — enrolling alone does not set the primary "
            "cohort. Nulled when nonzero but below the anonymization floor."
        )
    )
    certificates_earned: int | None = Field(
        description=(
            "Total certificates earned in the month (an event count, not distinct learners). "
            "Derived from certified_learners and nulled whenever it is suppressed."
        )
    )
    certified_learners: int | None = Field(
        description=(
            "Distinct learners who earned a certificate during the month. Nulled when nonzero "
            "but below the anonymization floor."
        )
    )
    total_videos_watched: int | None = Field(
        description=(
            "Total video-watch events in the month. Derived from video_watchers and nulled "
            "whenever it is suppressed."
        )
    )
    video_watchers: int | None = Field(
        description=(
            "Distinct learners who watched a video during the month. Nulled when nonzero but "
            "below the anonymization floor."
        )
    )
    total_problems_attempted: int | None = Field(
        description=(
            "Total problem-attempt events in the month. Derived from problem_attempters and "
            "nulled whenever it is suppressed."
        )
    )
    problem_attempters: int | None = Field(
        description=(
            "Distinct learners who attempted a problem during the month. Nulled when nonzero "
            "but below the anonymization floor."
        )
    )
    total_chatbot_interactions: int | None = Field(
        description=(
            "Total chatbot-interaction events in the month. Derived from chatbot_users and "
            "nulled whenever it is suppressed."
        )
    )
    chatbot_users: int | None = Field(
        description=(
            "Distinct learners who used the chatbot during the month. Nulled when nonzero but "
            "below the anonymization floor."
        )
    )


class ProgramFunnel(SQLModel):
    """mv_b2b_program_funnel — grain: org x contract x program.

    ``total_courses`` counts courses, not learners, so it is not a cohort.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="enrolled_in_contract_courses",
        secondary=("enrolled_via_program", "program_course_completers"),
    )

    organization_key: str = Field(
        description="Internal surrogate key identifying the organization."
    )
    organization_name: str = Field(description="Display name of the organization.")
    contract_pk: str = Field(description="Surrogate primary key of the B2B contract.")
    contract_id: int = Field(
        description="Numeric identifier of the B2B contract in the source system."
    )
    b2b_contract_name: str = Field(description="Name of the B2B contract.")
    program_pk: str = Field(description="Surrogate primary key of the program.")
    program_title: str = Field(description="Title of the program.")
    total_courses: int = Field(
        description=(
            "Number of distinct courses in the program that the contract covers. Not a learner "
            "cohort, so exempt from the anonymization floor."
        )
    )
    enrolled_in_contract_courses: int = Field(
        description=(
            "Distinct learners enrolled in any contract-covered course belonging to the "
            "program. The row's primary cohort: a row whose count is below the anonymization "
            "floor is withheld entirely."
        )
    )
    enrolled_via_program: int | None = Field(
        description=(
            "Distinct enrolled_in_contract_courses learners who enrolled through the program "
            "pathway itself, rather than directly in one of its courses. Nulled when nonzero "
            "but below the anonymization floor."
        )
    )
    program_course_completers: int | None = Field(
        description=(
            "Distinct learners who earned a non-revoked certificate in any contract-covered "
            "course of the program. An approximation of program completion — it counts a "
            "certificate in any program course, not a true program-level certificate — pending "
            "a dedicated program-certificate fact table. Nulled when nonzero but below the "
            "anonymization floor."
        )
    )


class ContentEngagementDepth(SQLModel):
    """mv_b2b_content_engagement_depth — grain: org x course_run (all-time).

    The chatbot columns are exact: ``total_chatbot_interactions`` sums over,
    and ``chatbot_adoption_pct`` divides by, ``chatbot_users`` — which this
    view does emit, so both are correctly floored. ``engagement_rate_pct`` is
    ``engaged_learners / total_enrolled_learners``, also correct.

    The video and problem columns are floored through the cohorts the view now
    publishes (ol-data-platform PR #2520): ``total_videos_watched`` is summed
    over ``video_watchers`` and ``total_problems_attempted`` over
    ``problem_attempters``, each a strict subset of ``engaged_learners``
    because watching a video or attempting a problem is one of the activities
    that sets ``active_count``. (Every cohort this view emits is such a
    subset. That is a property of these particular cohorts, not a general
    rule — see ``MonthlyEngagementTrend``, where ``enrolling_learners`` is
    not a subset of its primary because enrolling does not set
    ``active_count``.)

    The ``avg_*_per_engaged_learner`` columns are derived from *two* cohorts,
    which is why each names both. The denominator is ``engaged_learners`` —
    that is what the dbt SQL divides by, so the naming is now accurate — but
    the numerator is the activity SUM, contributed by only the narrower
    cohort. Mapping the average to its denominator alone would leave the
    numerator recoverable: an unsuppressed average multiplied by a published
    ``engaged_learners`` yields the suppressed total exactly, and when the
    contributing cohort is a single learner that total *is* that learner's
    value. Naming both cohorts nulls the average whenever either is sub-floor.

    ``certificates_earned`` is the one column still floored as a count of
    itself: it is ``sum(certificate_count)``, an event count, and this view
    emits no certified-learner cohort to attribute it to (unlike
    ``MonthlyEngagementTrend``, which has ``certified_learners``). Flooring an
    event count is weaker than flooring a cohort — several certificates can
    come from one learner — but strictly better than not flooring it. Emitting
    the cohort from dbt would close this the same way #2520 closed the others.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=(
            "engaged_learners",
            "video_watchers",
            "problem_attempters",
            "chatbot_users",
            "certificates_earned",
        ),
        derived={
            "engagement_rate_pct": ("engaged_learners",),
            "total_videos_watched": ("video_watchers",),
            "avg_videos_per_engaged_learner": ("engaged_learners", "video_watchers"),
            "total_problems_attempted": ("problem_attempters",),
            "avg_problems_per_engaged_learner": ("engaged_learners", "problem_attempters"),
            "total_chatbot_interactions": ("chatbot_users",),
            "chatbot_adoption_pct": ("chatbot_users",),
        },
    )

    organization_key: str = Field(
        description="Internal surrogate key identifying the organization."
    )
    organization_name: str = Field(description="Display name of the organization.")
    courserun_readable_id: str = Field(
        description="Human-readable identifier of the course run (course + run tag)."
    )
    courserun_title: str = Field(description="Title of the course run.")
    total_enrolled_learners: int = Field(
        description=(
            "Distinct learners ever enrolled in the course run. The row's primary cohort: a "
            "row whose count is below the anonymization floor is withheld entirely."
        )
    )
    engaged_learners: int | None = Field(
        description=(
            "Distinct total_enrolled_learners with any activity — navigation, discussion, "
            "video, problem, chatbot, or certificate. Nulled when nonzero but below the "
            "anonymization floor."
        )
    )
    engagement_rate_pct: float | None = Field(
        description=(
            "engaged_learners as a percentage of total_enrolled_learners. Nulled whenever "
            "engaged_learners is suppressed."
        )
    )
    total_videos_watched: int | None = Field(
        description=(
            "Total video-watch events for the course run. Derived from video_watchers and "
            "nulled whenever it is suppressed."
        )
    )
    video_watchers: int | None = Field(
        description=(
            "Distinct learners who watched a video in the course run. Nulled when nonzero but "
            "below the anonymization floor."
        )
    )
    avg_videos_per_engaged_learner: float | None = Field(
        description=(
            "total_videos_watched divided by engaged_learners. Nulled whenever either "
            "video_watchers or engaged_learners is suppressed."
        )
    )
    total_problems_attempted: int | None = Field(
        description=(
            "Total problem-attempt events for the course run. Derived from problem_attempters "
            "and nulled whenever it is suppressed."
        )
    )
    problem_attempters: int | None = Field(
        description=(
            "Distinct learners who attempted a problem in the course run. Nulled when nonzero "
            "but below the anonymization floor."
        )
    )
    avg_problems_per_engaged_learner: float | None = Field(
        description=(
            "total_problems_attempted divided by engaged_learners. Nulled whenever either "
            "problem_attempters or engaged_learners is suppressed."
        )
    )
    total_chatbot_interactions: int | None = Field(
        description=(
            "Total chatbot-interaction events for the course run. Derived from chatbot_users "
            "and nulled whenever it is suppressed."
        )
    )
    chatbot_users: int | None = Field(
        description=(
            "Distinct learners who used the chatbot in the course run. Nulled when nonzero but "
            "below the anonymization floor."
        )
    )
    chatbot_adoption_pct: float | None = Field(
        description=(
            "chatbot_users as a percentage of total_enrolled_learners. Nulled whenever "
            "chatbot_users is suppressed."
        )
    )
    certificates_earned: int | None = Field(
        description=(
            "Total certificates earned for the course run (an event count, not distinct "
            "learners; this view emits no certified-learner cohort to attribute it to). Nulled "
            "when nonzero but below the anonymization floor."
        )
    )


class MitAdminContractHealth(SQLModel):
    """mv_b2b_mit_admin_contract_health — grain: org x contract (MIT admin only)."""

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="seats_consumed",
        secondary=("active_learners", "certified_learners"),
        derived={"completion_rate_pct": ("certified_learners",)},
    )

    organization_key: str = Field(
        description="Internal surrogate key identifying the organization."
    )
    organization_name: str = Field(description="Display name of the organization.")
    contract_pk: str = Field(description="Surrogate primary key of the B2B contract.")
    contract_id: int = Field(
        description="Numeric identifier of the B2B contract in the source system."
    )
    b2b_contract_name: str = Field(description="Name of the B2B contract.")
    b2b_contract_is_active: bool = Field(description="Whether the contract is currently active.")
    b2b_contract_start_date: datetime.date | None = Field(
        description="Date the contract's coverage begins, or null if unset."
    )
    b2b_contract_end_date: datetime.date | None = Field(
        description="Date the contract's coverage ends, or null if the contract has no end date."
    )
    seat_limit: int | None = Field(
        description=(
            "Maximum number of seats the contract allows. Zero or null means unlimited, and "
            "seat_utilization_pct is null for both."
        )
    )
    b2b_contract_membership_type: str | None = Field(
        description="Membership type configured for the contract, or null if not set."
    )
    seats_consumed: int = Field(
        description=(
            "Distinct learners enrolled in any course run covered by the contract. The row's "
            "primary cohort: a row whose count is below the anonymization floor is withheld "
            "entirely."
        )
    )
    active_learners: int | None = Field(
        description=(
            "Distinct seats_consumed learners with a currently active enrollment. Nulled when "
            "nonzero but below the anonymization floor."
        )
    )
    certified_learners: int | None = Field(
        description=(
            "Distinct seats_consumed learners who earned a non-revoked certificate. Nulled "
            "when nonzero but below the anonymization floor."
        )
    )
    seat_utilization_pct: float | None = Field(
        description="seats_consumed as a percentage of seat_limit."
    )
    completion_rate_pct: float | None = Field(
        description=(
            "certified_learners as a percentage of seats_consumed. Nulled whenever "
            "certified_learners is suppressed."
        )
    )
    health_status: str = Field(
        description=(
            "Coarse contract-health classification: 'inactive' if the contract is not active; "
            "else 'high_utilization' when seat_utilization_pct is at least 90; 'at_risk' when "
            "seat_utilization_pct is below 25 and the contract ends within 90 days; 'healthy' "
            "when seat_utilization_pct is at least 50; otherwise 'early_stage'."
        )
    )


class ContractMonthlyEngagementTrend(MonthlyEngagementTrend):
    """mv_b2b_contract_monthly_engagement_trend — grain: org x contract x month.

    The contract-scoped sibling of ``MonthlyEngagementTrend``, backing the
    endpoints nested under a contract. Subclassed rather than redeclared so the
    two can't drift: the column set and the ``cohort_policy`` — which is what
    the anonymization floor reads — are inherited verbatim, and only contract
    identity is added. The dbt models are siblings in the same way.

    The contract columns are not cohorts and take no part in the policy.

    A learner active under two of an org's contracts appears in both rows, so
    these rows do not partition the org-level view's learner counts; summing
    ``monthly_active_learners`` across contracts can exceed the org's own
    figure. Activity totals, being sums of events, do add up.
    """

    contract_pk: str = Field(description="Surrogate primary key of the B2B contract.")
    contract_id: int = Field(
        description="Numeric identifier of the B2B contract in the source system."
    )
    b2b_contract_name: str = Field(description="Name of the B2B contract.")


class ContractContentEngagementDepth(ContentEngagementDepth):
    """mv_b2b_contract_content_engagement_depth — grain: org x contract x run.

    The contract-scoped sibling of ``ContentEngagementDepth``, inherited for
    the same reason as ``ContractMonthlyEngagementTrend``.

    Unlike the trend view, these rows ARE a strict partition of the org-level
    view: a course run belongs to exactly one contract, so naming the contract
    labels a row rather than splitting it, and every count here equals its
    org-level counterpart for the same course run. That equality is exactly
    what makes a suppressed contract recoverable by subtraction once an org
    holds more than one — the k-anonymity floor here is per-row and does not
    defend against differencing across the two grains.
    """

    contract_pk: str = Field(description="Surrogate primary key of the B2B contract.")
    contract_id: int = Field(
        description="Numeric identifier of the B2B contract in the source system."
    )
    b2b_contract_name: str = Field(description="Name of the B2B contract.")
