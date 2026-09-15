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

Field descriptions are written for an organization's managers, because the
dashboard can show them as help text: plain language, no field names. Which
counts are withheld, and what each rate is derived from, is defined by the
model's ``cohort_policy`` and explained in its docstring rather than repeated
per field.
"""

from __future__ import annotations

import datetime
from typing import ClassVar

from pydantic import BaseModel
from sqlmodel import Field, SQLModel

from ol_analytics_api.core.anonymization import CohortPolicy

# The same wording the MIT Learn dashboard uses for a withheld figure.
_WITHHELD = "Withheld when too few learners are in the group to report without identifying them."
_ROW_WITHHELD = (
    "When too few learners are in the group, the whole row is withheld to avoid identifying them."
)
_DERIVED_WITHHELD = "Withheld when the learner count it is based on is withheld."
_ANY_ACTIVITY = (
    "watched a video, attempted a problem, posted in a discussion, used the chatbot, moved "
    "through course pages or earned a certificate"
)


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
    """mv_b2b_contract_utilization — grain: org x contract.

    ``seats_consumed`` is the primary cohort. ``active_learners`` and
    ``learners_certified`` are secondary counts, nulled when nonzero but below
    the floor. ``completion_rate_pct`` is ``learners_certified`` over
    ``seats_consumed`` and is nulled with it. ``seat_utilization_pct`` is
    ``seats_consumed`` over ``seat_limit``, null when the limit is zero or null
    (the view divides by ``nullif(seat_limit, 0)``).
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="seats_consumed",
        secondary=("active_learners", "learners_certified"),
        derived={"completion_rate_pct": ("learners_certified",)},
    )

    organization_key: str = Field(description="Internal identifier for the organization.")
    organization_name: str = Field(description="The organization's name.")
    contract_pk: str = Field(description="Internal identifier for the contract.")
    contract_id: int = Field(description="The contract's ID in MITx Online.")
    b2b_contract_name: str = Field(description="The contract's name.")
    b2b_contract_is_active: bool = Field(description="Whether the contract is currently active.")
    b2b_contract_start_date: datetime.date | None = Field(
        description="When the contract starts. Empty if no start date is set."
    )
    b2b_contract_end_date: datetime.date | None = Field(
        description="When the contract ends. Empty if it has no end date."
    )
    seat_limit: int | None = Field(
        description="How many seats the contract includes. Empty or zero means unlimited."
    )
    b2b_contract_membership_type: str | None = Field(
        description="The contract's membership type. Empty if not set."
    )
    seats_consumed: int = Field(
        description=f"Learners enrolled in at least one course under the contract. {_ROW_WITHHELD}"
    )
    active_learners: int | None = Field(
        description=f"Learners on the contract whose enrollment is still active. {_WITHHELD}"
    )
    learners_certified: int | None = Field(
        description=(
            f"Learners on the contract who earned a certificate that hasn't been revoked. "
            f"{_WITHHELD}"
        )
    )
    seat_utilization_pct: float | None = Field(
        description=(
            "Percentage of the contract's seats in use. Empty when the contract has unlimited "
            "seats."
        )
    )
    completion_rate_pct: float | None = Field(
        description=(
            f"Percentage of enrolled learners who earned a certificate. {_DERIVED_WITHHELD}"
        )
    )


class EnrollmentCompletionFunnel(SQLModel):
    """mv_b2b_enrollment_completion_funnel — grain: org x contract x course_run.

    ``enrolled_learners`` is the primary cohort. ``active_rate_pct`` and
    ``completion_rate_pct`` are ``active_learners`` and ``certified_learners``
    over ``enrolled_learners``, each nulled with its numerator.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="enrolled_learners",
        secondary=("active_learners", "passing_learners", "certified_learners"),
        derived={
            "active_rate_pct": ("active_learners",),
            "completion_rate_pct": ("certified_learners",),
        },
    )

    organization_key: str = Field(description="Internal identifier for the organization.")
    organization_name: str = Field(description="The organization's name.")
    contract_pk: str = Field(description="Internal identifier for the contract.")
    contract_id: int = Field(description="The contract's ID in MITx Online.")
    b2b_contract_name: str = Field(description="The contract's name.")
    courserun_pk: str = Field(description="Internal identifier for the course run.")
    courserun_readable_id: str = Field(
        description="The course run's ID, e.g. course-v1:MITxT+14.310x+2T2026."
    )
    courserun_title: str = Field(description="The course's title.")
    enrolled_learners: int = Field(
        description=f"Learners enrolled in this course run. {_ROW_WITHHELD}"
    )
    active_learners: int | None = Field(
        description=f"Enrolled learners whose enrollment is still active. {_WITHHELD}"
    )
    passing_learners: int | None = Field(
        description=f"Enrolled learners with a passing grade. {_WITHHELD}"
    )
    certified_learners: int | None = Field(
        description=(
            f"Enrolled learners who earned a certificate that hasn't been revoked. {_WITHHELD}"
        )
    )
    active_rate_pct: float | None = Field(
        description=(
            f"Percentage of enrolled learners whose enrollment is still active. {_DERIVED_WITHHELD}"
        )
    )
    completion_rate_pct: float | None = Field(
        description=(
            f"Percentage of enrolled learners who earned a certificate. {_DERIVED_WITHHELD}"
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

    organization_key: str = Field(description="Internal identifier for the organization.")
    organization_name: str = Field(description="The organization's name.")
    activity_year_and_month: str = Field(description="The month, e.g. 2026-08.")
    monthly_active_learners: int = Field(
        description=(
            f"Learners who did anything in a course this month: {_ANY_ACTIVITY}. Enrolling "
            "alone doesn't count. If too few learners were active, the whole month is withheld "
            "to avoid identifying them."
        )
    )
    new_enrollments: int | None = Field(
        description=(
            "Course enrollments made this month. A learner who enrolled in six courses counts "
            f"six times. {_DERIVED_WITHHELD}"
        )
    )
    enrolling_learners: int | None = Field(
        description=f"Learners who enrolled in at least one course this month. {_WITHHELD}"
    )
    certificates_earned: int | None = Field(
        description=(
            "Certificates earned this month. A learner who earned two counts twice. "
            f"{_DERIVED_WITHHELD}"
        )
    )
    certified_learners: int | None = Field(
        description=f"Learners who earned at least one certificate this month. {_WITHHELD}"
    )
    total_videos_watched: int | None = Field(
        description=f"Videos watched this month, counting every view. {_DERIVED_WITHHELD}"
    )
    video_watchers: int | None = Field(
        description=f"Learners who watched at least one video this month. {_WITHHELD}"
    )
    total_problems_attempted: int | None = Field(
        description=f"Problem attempts this month, counting every attempt. {_DERIVED_WITHHELD}"
    )
    problem_attempters: int | None = Field(
        description=f"Learners who attempted at least one problem this month. {_WITHHELD}"
    )
    total_chatbot_interactions: int | None = Field(
        description=f"Chatbot interactions this month. {_DERIVED_WITHHELD}"
    )
    chatbot_users: int | None = Field(
        description=f"Learners who used the chatbot this month. {_WITHHELD}"
    )


class ProgramFunnel(SQLModel):
    """mv_b2b_program_funnel — grain: org x contract x program.

    ``total_courses`` counts courses, not learners, so it is not a cohort.

    ``program_course_completers`` approximates program completion: it counts a
    non-revoked certificate in any contract-covered course of the program, not
    a program-level certificate, pending a program-certificate fact table.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="enrolled_in_contract_courses",
        secondary=("enrolled_via_program", "program_course_completers"),
    )

    organization_key: str = Field(description="Internal identifier for the organization.")
    organization_name: str = Field(description="The organization's name.")
    contract_pk: str = Field(description="Internal identifier for the contract.")
    contract_id: int = Field(description="The contract's ID in MITx Online.")
    b2b_contract_name: str = Field(description="The contract's name.")
    program_pk: str = Field(description="Internal identifier for the program.")
    program_title: str = Field(description="The program's title.")
    total_courses: int = Field(description="Courses in the program that the contract covers.")
    enrolled_in_contract_courses: int = Field(
        description=(
            "Learners enrolled in at least one of the program's courses under the contract. "
            f"{_ROW_WITHHELD}"
        )
    )
    enrolled_via_program: int | None = Field(
        description=(
            "Of those learners, how many enrolled in the program itself rather than directly in "
            f"one of its courses. {_WITHHELD}"
        )
    )
    program_course_completers: int | None = Field(
        description=(
            "Learners who earned a certificate in at least one of the program's courses under "
            f"the contract. This isn't the same as completing the whole program. {_WITHHELD}"
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

    organization_key: str = Field(description="Internal identifier for the organization.")
    organization_name: str = Field(description="The organization's name.")
    courserun_readable_id: str = Field(
        description="The course run's ID, e.g. course-v1:MITxT+14.310x+2T2026."
    )
    courserun_title: str = Field(description="The course's title.")
    total_enrolled_learners: int = Field(
        description=f"Learners who have ever enrolled in this course run. {_ROW_WITHHELD}"
    )
    engaged_learners: int | None = Field(
        description=(
            f"Enrolled learners who did anything in the course: {_ANY_ACTIVITY}. {_WITHHELD}"
        )
    )
    engagement_rate_pct: float | None = Field(
        description=(
            f"Percentage of enrolled learners who did anything in the course. {_DERIVED_WITHHELD}"
        )
    )
    total_videos_watched: int | None = Field(
        description=(f"Videos watched in this course run, counting every view. {_DERIVED_WITHHELD}")
    )
    video_watchers: int | None = Field(
        description=f"Learners who watched at least one video in this course run. {_WITHHELD}"
    )
    avg_videos_per_engaged_learner: float | None = Field(
        description=(
            "Average videos watched per learner who did anything in the course. "
            f"{_DERIVED_WITHHELD}"
        )
    )
    total_problems_attempted: int | None = Field(
        description=(
            f"Problem attempts in this course run, counting every attempt. {_DERIVED_WITHHELD}"
        )
    )
    problem_attempters: int | None = Field(
        description=(f"Learners who attempted at least one problem in this course run. {_WITHHELD}")
    )
    avg_problems_per_engaged_learner: float | None = Field(
        description=(
            "Average problem attempts per learner who did anything in the course. "
            f"{_DERIVED_WITHHELD}"
        )
    )
    total_chatbot_interactions: int | None = Field(
        description=f"Chatbot interactions in this course run. {_DERIVED_WITHHELD}"
    )
    chatbot_users: int | None = Field(
        description=f"Learners who used the chatbot in this course run. {_WITHHELD}"
    )
    chatbot_adoption_pct: float | None = Field(
        description=f"Percentage of enrolled learners who used the chatbot. {_DERIVED_WITHHELD}"
    )
    certificates_earned: int | None = Field(
        description=f"Certificates earned in this course run. {_WITHHELD}"
    )


class MitAdminContractHealth(SQLModel):
    """mv_b2b_mit_admin_contract_health — grain: org x contract (MIT admin only).

    Floored like ``ContractUtilization``. ``health_status`` is computed in the
    view from ``b2b_contract_is_active``, ``seat_utilization_pct`` and
    ``b2b_contract_end_date``.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="seats_consumed",
        secondary=("active_learners", "certified_learners"),
        derived={"completion_rate_pct": ("certified_learners",)},
    )

    organization_key: str = Field(description="Internal identifier for the organization.")
    organization_name: str = Field(description="The organization's name.")
    contract_pk: str = Field(description="Internal identifier for the contract.")
    contract_id: int = Field(description="The contract's ID in MITx Online.")
    b2b_contract_name: str = Field(description="The contract's name.")
    b2b_contract_is_active: bool = Field(description="Whether the contract is currently active.")
    b2b_contract_start_date: datetime.date | None = Field(
        description="When the contract starts. Empty if no start date is set."
    )
    b2b_contract_end_date: datetime.date | None = Field(
        description="When the contract ends. Empty if it has no end date."
    )
    seat_limit: int | None = Field(
        description="How many seats the contract includes. Empty or zero means unlimited."
    )
    b2b_contract_membership_type: str | None = Field(
        description="The contract's membership type. Empty if not set."
    )
    seats_consumed: int = Field(
        description=f"Learners enrolled in at least one course under the contract. {_ROW_WITHHELD}"
    )
    active_learners: int | None = Field(
        description=f"Learners on the contract whose enrollment is still active. {_WITHHELD}"
    )
    certified_learners: int | None = Field(
        description=(
            f"Learners on the contract who earned a certificate that hasn't been revoked. "
            f"{_WITHHELD}"
        )
    )
    seat_utilization_pct: float | None = Field(
        description=(
            "Percentage of the contract's seats in use. Empty when the contract has unlimited "
            "seats."
        )
    )
    completion_rate_pct: float | None = Field(
        description=(
            f"Percentage of enrolled learners who earned a certificate. {_DERIVED_WITHHELD}"
        )
    )
    health_status: str = Field(
        description=(
            "Overall contract health: inactive (the contract isn't active), high_utilization "
            "(90% or more of seats in use), at_risk (under 25% of seats in use and the contract "
            "ends within 90 days), healthy (50% or more of seats in use), or early_stage "
            "(anything else)."
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

    contract_pk: str = Field(description="Internal identifier for the contract.")
    contract_id: int = Field(description="The contract's ID in MITx Online.")
    b2b_contract_name: str = Field(description="The contract's name.")


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

    contract_pk: str = Field(description="Internal identifier for the contract.")
    contract_id: int = Field(description="The contract's ID in MITx Online.")
    b2b_contract_name: str = Field(description="The contract's name.")
