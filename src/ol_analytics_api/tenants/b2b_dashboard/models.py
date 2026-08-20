"""Response schemas mirroring the 6 StarRocks B2B analytics materialized views.

Column sets match the dbt models in `ol-data-platform`'s
`models/b2b_analytics/*.sql` (mitodl/ol-data-platform PR #2329) exactly.
These are plain SQLModel (Pydantic) schemas, not ORM tables — StarRocks-side
schema is owned by dbt, not by this service.

Every row model declares a ``cohort_policy`` (see core.anonymization): the
distinct-entity counts subject to the k-anonymity floor, which of them sit
inside which, and the derived values computed over them. The response layer
nulls sub-floor secondary counts, counts whose complement within a containing
cohort is sub-floor, and the derivatives of both — so any count/rate/average
column that can be suppressed is typed Optional even though the view never
emits a NULL there.
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

    organization_key: str
    organization_name: str
    contract_pk: str
    contract_id: str
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

    organization_key: str
    organization_name: str
    contract_pk: str
    contract_id: str
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

    organization_key: str
    organization_name: str
    activity_year_and_month: str
    monthly_active_learners: int
    new_enrollments: int | None
    enrolling_learners: int | None
    certificates_earned: int | None
    certified_learners: int | None
    total_videos_watched: int | None
    video_watchers: int | None
    total_problems_attempted: int | None
    problem_attempters: int | None
    total_chatbot_interactions: int | None
    chatbot_users: int | None


class ProgramFunnel(SQLModel):
    """mv_b2b_program_funnel — grain: org x contract x program.

    ``total_courses`` counts courses, not learners, so it is not a cohort.
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

    organization_key: str
    organization_name: str
    contract_pk: str
    contract_id: str
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

    organization_key: str
    organization_name: str
    courserun_readable_id: str
    courserun_title: str
    total_enrolled_learners: int
    engaged_learners: int | None
    engagement_rate_pct: float | None
    total_videos_watched: int | None
    video_watchers: int | None
    avg_videos_per_engaged_learner: float | None
    total_problems_attempted: int | None
    problem_attempters: int | None
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
        # Same shape as ContractUtilization: both are counted off the
        # contract's own enrollment rows, so both are subsets of the seats
        # consumed.
        contained_in={
            "active_learners": "seats_consumed",
            "certified_learners": "seats_consumed",
        },
    )

    organization_key: str
    organization_name: str
    contract_pk: str
    contract_id: str
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
    figure. Activity totals, being sums of events, do add up — which is what
    makes a contract-month the floor withholds recoverable from the org
    endpoint as ``org_total - sum(the visible contract months)``. The org
    endpoint defends against that itself: it probes this view for the months
    it withholds and blanks its own additive totals for them (see
    ``routers.organizations._FinerGrain``). The learner counts are left alone,
    because not adding up is exactly what stops them from being recovered by
    subtraction.
    """

    contract_pk: str
    contract_id: str
    b2b_contract_name: str


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

    contract_pk: str
    contract_id: str
    b2b_contract_name: str
