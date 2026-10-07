"""Response schemas for this tenant's aggregate endpoints.

Six of them mirror a StarRocks B2B analytics materialized view, and their
column sets match the dbt models in `ol-data-platform`'s
`models/b2b_analytics/*.sql` (mitodl/ol-data-platform PR #2329) exactly.
These are plain SQLModel (Pydantic) schemas, not ORM tables — StarRocks-side
schema is owned by dbt, not by this service.

``ContractNeedsAttention`` is the exception: it is aggregated at query time in
this service rather than by dbt, for reasons its own docstring gives. What
makes it belong in this module is the ``cohort_policy`` below, not the MV it
doesn't have.

Every row model declares a ``cohort_policy`` (see core.anonymization): the
distinct-entity counts subject to the k-anonymity floor, which of them sit
inside which, and the derived values computed over them. The response layer
nulls sub-floor secondary counts, counts whose complement within a containing
cohort is sub-floor, and the derivatives of both — so any count/rate/average
column that can be suppressed is typed Optional even though the view never
emits a NULL there.

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
    "watched a video, attempted a problem, posted in a discussion, used the chatbot or moved "
    "through course pages"
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
        # Both are counted from users the contract's enrollments already
        # produced (`active_learners` filters those enrollments;
        # `learners_certified` counts certificates on the same contract's
        # course runs, which a learner can only hold by enrolling), so each is
        # a subset of the seats consumed.
        contained_in={
            "active_learners": "seats_consumed",
            "learners_certified": "seats_consumed",
        },
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


class ContractNeedsAttention(SQLModel):
    """Distinct learners needing attention — grain: org x contract.

    The one model here that does not mirror a materialized view. It is computed
    at query time by ``learner_queries.needs_attention_aggregate`` over
    ``b2b_learner_records.mv_b2b_learner_enrollment``, which is what lets the
    KPI tile and the learner directory beneath it share a single
    needs-attention expression and a single per-request cutoff.

    A column on ``mv_b2b_contract_utilization`` was the other candidate and was
    not chosen (decided 2026-10-02). It would have restated the 30-day rule and
    the completion-status CASE in dbt — two repos, two languages, no test that
    can see both — and frozen the cutoff at MV-refresh time, so the tile and the
    directory could disagree about the same learner for up to a refresh
    interval. The rule has already changed once since it was written.

    Served from its own endpoint rather than folded into ``ContractUtilization``
    because the MV behind it refreshes on its own schedule. One ``as_of`` per
    section is exactly what stops a lagging view from making another section
    look fresher than it is, so a client renders this tile's freshness from
    this endpoint's envelope, not from contract-utilization's.

    ``learners_considered`` is the primary cohort and gates the row. The other
    two counts are secondary, nulled on their own terms and when their
    complement within ``learners_considered`` is under the floor: 42 considered
    and 40 needing attention names the 2 who do not, so the 40 is withheld. A
    client has to render that as withheld, not as nobody needing attention.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="learners_considered",
        secondary=("learners_needing_attention", "learners_outcomes_withheld"),
        # All three are COUNT(DISTINCT learner_id) over the same rows of one
        # subquery (learner_queries.needs_attention_aggregate), the two
        # secondaries under a CASE, so each is a subset of the considered.
        contained_in={
            "learners_needing_attention": "learners_considered",
            "learners_outcomes_withheld": "learners_considered",
        },
    )

    contract_id: int = Field(description="The contract's ID in MITx Online.")
    learners_considered: int = Field(
        description=(
            "Learners with an active enrollment under the contract. Deactivated enrollments, "
            f"such as after unenrolling or a refund, are left out. {_ROW_WITHHELD}"
        )
    )
    learners_needing_attention: int | None = Field(
        description=(
            "Of those learners, how many may need a nudge: they never started a course, or "
            "their last recorded activity was at least 30 days ago. A learner counts once "
            "however many of the contract's courses they are behind in. Learners who haven't "
            f"agreed to share their progress aren't counted. {_WITHHELD}"
        )
    )
    learners_outcomes_withheld: int | None = Field(
        description=(
            "Of those learners, how many are left out of the count above because they haven't "
            f"agreed to share their progress. {_WITHHELD}"
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
        # All three are counted off the enrollment row itself — grades and
        # certificates join on `(user, course_run)` from the enrollment — so
        # each names a subset of the enrolled learners. They are declared flat
        # under the primary rather than chained (certified inside passing
        # inside active): the view's SQL does not enforce those inner
        # containments, and declaring one that does not hold fails closed and
        # would suppress good data.
        contained_in={
            "active_learners": "enrolled_learners",
            "passing_learners": "enrolled_learners",
            "certified_learners": "enrolled_learners",
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

    ``contributing_learners`` is the primary cohort and gates the row. It
    counts every learner behind the month: active, enrolling or certified
    (ol-data-platform PR #2881). Every other learner count in the row is a
    subset of it. It is selected for the gate and excluded from the response.

    ``monthly_active_learners`` is not the gate, because activity is course
    work only. Enrolling or being issued a certificate does not make a learner
    active, so ``enrolling_learners`` and ``certified_learners`` are not
    subsets of it, and a month can have enough of either with too few active
    learners. Gating on it would withhold those figures for no privacy
    reason. It is a secondary count, nulled on its own terms.

    No total is attributable to the primary. Each is a SUM contributed by only
    the learners who did that specific thing, and clearing the primary floor
    says nothing about whether that narrower cohort cleared it. A month with
    40 contributing learners can carry a chatbot total from exactly one of
    them, which is why each total is ``derived`` from its own cohort.

    ``new_enrollments`` and ``certificates_earned`` are SUMs of
    per-learner-per-course-run markers, so they count *events*, not learners:
    one learner enrolling in six runs reads as ``new_enrollments == 6`` and
    would clear a floor of 5 on its own. Flooring them directly is therefore
    the wrong instrument — they are ``derived`` from ``enrolling_learners``
    and ``certified_learners``, the distinct-learner counts they are actually
    attributable to, which do carry the floor.

    ``monthly_active_learners`` is Optional even though it is the primary —
    everywhere else the primary gates the row (below floor, the row is dropped
    whole, never nulled) rather than being nulled itself. The org grain is the
    exception: it is also this endpoint's ``_FinerGrain.guarded_cohorts``
    target, so a month whose contract-level breakdown hides anything gets its
    org-level ``monthly_active_learners`` blanked post hoc, after its own row
    gate already passed. See ``routers.organizations`` and
    ``ContractMonthlyEngagementTrend``.
    """

    cohort_policy: ClassVar[CohortPolicy] = CohortPolicy(
        primary="contributing_learners",
        secondary=(
            "monthly_active_learners",
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
        # Earning a certificate, watching a video, attempting a problem and
        # using the chatbot each set `active_count`, so all four cohorts are
        # subsets of the month's active learners and their complements are
        # real: 42 active of whom 40 used the chatbot names the 2 who did not.
        contained_in={
            "certified_learners": "monthly_active_learners",
            "video_watchers": "monthly_active_learners",
            "problem_attempters": "monthly_active_learners",
            "chatbot_users": "monthly_active_learners",
        },
        # Enrolling does not set `active_count`, so a learner who only enrolled
        # is counted here and not in the primary. `monthly_active_learners -
        # enrolling_learners` is therefore not a complement — it can even go
        # negative — and reading it as one would suppress on noise.
        uncontained=("enrolling_learners",),
    )

    organization_key: str = Field(description="Internal identifier for the organization.")
    organization_name: str = Field(description="The organization's name.")
    activity_year_and_month: str = Field(description="The month, e.g. 2026-08.")
    monthly_active_learners: int | None = Field(
        description=(
            f"Learners who did course work this month: {_ANY_ACTIVITY}. Enrolling or earning "
            f"a certificate alone doesn't count. {_WITHHELD}"
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
    # Selected so the row can be gated on it, never returned. Next to the other
    # counts it would pin a withheld one: 16 contributing with 12 certified
    # published and the active count withheld means exactly 4 were active.
    contributing_learners: int = Field(
        exclude=True,
        description="Learners who did course work, enrolled or earned a certificate this month.",
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
        # Both are counted off the same enrollment rows as the primary — one
        # filtered to the program pathway, one joined to certificates on
        # `(user, course_run)` — so each is a subset of it.
        contained_in={
            "enrolled_via_program": "enrolled_in_contract_courses",
            "program_course_completers": "enrolled_in_contract_courses",
        },
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
    because watching a video or attempting a problem is tracked activity.
    ``chatbot_users`` is one too. That is a property of these particular
    cohorts, not a general rule: a certified learner need not be engaged,
    and see ``MonthlyEngagementTrend``, where neither ``enrolling_learners``
    nor ``certified_learners`` is a subset of ``monthly_active_learners``.

    The ``avg_*_per_engaged_learner`` columns are derived from *two* cohorts,
    which is why each names both. The denominator is ``engaged_learners`` —
    that is what the dbt SQL divides by, so the naming is now accurate — but
    the numerator is the activity SUM, contributed by only the narrower
    cohort. Mapping the average to its denominator alone would leave the
    numerator recoverable: an unsuppressed average multiplied by a published
    ``engaged_learners`` yields the suppressed total exactly, and when the
    contributing cohort is a single learner that total *is* that learner's
    value. Naming both cohorts nulls the average whenever either is sub-floor.

    ``certificates_earned`` is floored as a count of itself. Since
    ol-data-platform PR #2881 it is one per enrolled learner holding an
    unrevoked certificate for the run, so it counts learners as long as a
    course run belongs to one contract (the dbt bridge does not test that).
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
        # Nested two deep, and the inner level is the one that bites: watching
        # a video sets `active_count`, so the watchers sit inside the engaged
        # learners, and 40 watchers of 42 engaged names the 2 engaged learners
        # who never watched one. The outer pair is walked transitively, so the
        # complement against total enrollment is checked too.
        contained_in={
            "engaged_learners": "total_enrolled_learners",
            "video_watchers": "engaged_learners",
            "problem_attempters": "engaged_learners",
            "chatbot_users": "engaged_learners",
        },
        # `sum(certificate_count)` counts certificates, not learners: one
        # learner can hold several, so it is not a subset of any cohort here
        # and can exceed one. It stays floored as a count of itself (see
        # above); a complement rule over it would be arithmetic on two
        # different units.
        uncontained=("certificates_earned",),
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
        # Same shape as ContractUtilization: both are counted off the
        # contract's own enrollment rows, so both are subsets of the seats
        # consumed.
        contained_in={
            "active_learners": "seats_consumed",
            "certified_learners": "seats_consumed",
        },
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
    these rows do not partition the org-level view's learner counts in
    general; summing ``monthly_active_learners`` across contracts can exceed
    the org's own figure. Activity totals, being sums of events, always add up
    — which is what makes a contract-month the floor withholds recoverable
    from the org endpoint as ``org_total - sum(the visible contract months)``.
    The org endpoint defends against that itself: it probes this view for the
    months it withholds and blanks its own additive totals for them (see
    ``routers.organizations._FinerGrain``).

    The learner counts don't get to skip that defense on the strength of "not
    adding up in general": two contracts that happen to share no learners *do*
    add up exactly, and a hidden one comes back from the visible sibling's
    total the same as a hidden event sum would. Nothing here can tell that
    case from an overlapping one, so the org endpoint guards every cohort
    column — not just the additive totals — for any month it hides anything
    for (``CrossGrainAdditives.guarded_cohorts``), accepting the cost of
    blanking counts that overlap would have made safe to publish.
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
    org-level counterpart for the same course run.

    That equality is why this pair needs no cross-grain guard, where the trend
    pair does. Nothing is aggregated away going from contract grain to org
    grain, so there is no remainder to subtract: a course run's org row and its
    contract row hold the same numbers, the floor makes the same call on both,
    and a caller reading one learns nothing the other withholds.
    """

    contract_pk: str = Field(description="Internal identifier for the contract.")
    contract_id: int = Field(description="The contract's ID in MITx Online.")
    b2b_contract_name: str = Field(description="The contract's name.")
