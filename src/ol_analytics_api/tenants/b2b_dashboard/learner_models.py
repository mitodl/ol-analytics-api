"""Response schemas for the learner-progress endpoint.

Kept apart from models.py, where every row model declares a ``cohort_policy``.
These rows identify individual learners, so no floor applies and nothing here
imports core/anonymization.py.

Consent gates outcome fields, not the row. The query already projects NULL for
withheld outcomes (learner_queries._outcomes_shared); the validator below is a
second check, so a query change that projects a raw outcome column still can't
disclose it.

Field descriptions are written for an organization's managers, because the
dashboard can show them as help text: plain language, no field names. The
developer detail lives here instead:

- ``learner_id`` is the Keycloak user id (MITx Online's user ``global_id``). It
  is stable across email changes, so join against MITx Online's manager
  enrollment list on it, never on ``email``.
- ``enrollment_is_active`` and the enrollment fields are not consent-gated;
  everything in ``_OUTCOME_FIELDS`` is.
- ``completion_status``: an unrevoked certificate is ``certified``. A revoked
  certificate doesn't count, and the status then follows the grade, so it can
  read ``passed``, ``in_progress`` or ``not_started``. Until learner-grain
  activity data lands, ``in_progress`` means a nonzero grade.
- ``last_active_on`` is NULL for every row until activity data lands, whatever
  ``outcomes_shared`` says.
- ``outcomes_withheld_count`` counts the rows in ``total_count`` whose
  ``outcomes_shared`` is false. ``completion_status_counts`` buckets the rest
  by status; the two together add up to ``total_count``, since
  ``CompletionStatus`` is exhaustive and its branches don't overlap.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator


def _assume_utc(value: datetime.datetime) -> datetime.datetime:
    # StarRocks returns these timestamps as zone-less UTC strings. Without an
    # offset a browser reads them as local time and shifts the displayed dates.
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)


UtcDatetime = Annotated[datetime.datetime, AfterValidator(_assume_utc)]

_OUTCOME_FIELDS = (
    "completion_status",
    "is_passing",
    "grade",
    "letter_grade",
    "certificate_issued_on",
    "certificate_is_revoked",
    "last_active_on",
)

_HIDDEN = "Hidden if the learner hasn't agreed to share their progress."


class CompletionStatus(StrEnum):
    """``passed`` without ``certified`` is normal: certificates are issued on a
    schedule after grading, and audit-mode enrollments never certify."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    CERTIFIED = "certified"


class LearnerProgress(BaseModel):
    """One learner's enrollment in one course run under the contract."""

    learner_id: str = Field(
        description="The learner's account ID. It stays the same if their email address changes."
    )
    email: str | None = Field(
        description="The learner's email address. It may differ from the one they enrolled with."
    )
    full_name: str | None = Field(description="The learner's name, if they've provided one.")
    courserun_readable_id: str = Field(
        description="The course run's ID, e.g. course-v1:MITxT+14.310x+2T2026."
    )
    courserun_title: str = Field(description="The course's title.")
    courserun_start_on: UtcDatetime | None = Field(
        description="When the course run starts. Empty if no start date is set."
    )
    courserun_end_on: UtcDatetime | None = Field(
        description="When the course run ends. Empty for self-paced courses."
    )
    enrolled_on: UtcDatetime = Field(description="When the learner enrolled in this course run.")
    enrollment_is_active: bool = Field(
        description=(
            "Whether the learner is still enrolled. False if the enrollment was deactivated, "
            "for example after unenrolling."
        )
    )
    enrollment_mode: str | None = Field(
        description=(
            "The enrollment track, for example verified or audit. Audit enrollments don't earn "
            "certificates."
        )
    )
    outcomes_shared: bool = Field(
        description=(
            "Whether the learner has agreed to share their progress. If not, their status, "
            "grades, certificate and activity are hidden."
        )
    )
    completion_status: CompletionStatus | None = Field(
        description=(
            "Where the learner is in the course: not started, in progress, passed or certified. "
            f"{_HIDDEN}"
        )
    )
    is_passing: bool | None = Field(
        description=(
            "Whether the learner currently has a passing grade. Empty if no grade has been "
            f"calculated yet. {_HIDDEN}"
        )
    )
    grade: float | None = Field(description=f"The learner's current grade, from 0 to 1. {_HIDDEN}")
    letter_grade: str | None = Field(
        description=f"The learner's letter grade, when the course assigns one. {_HIDDEN}"
    )
    certificate_issued_on: UtcDatetime | None = Field(
        description=(
            f"When the learner's certificate was issued. Empty if they don't have one. {_HIDDEN}"
        )
    )
    certificate_is_revoked: bool | None = Field(
        description=(
            "Whether the learner's certificate was revoked. A revoked certificate doesn't count "
            f"toward completion. Empty if they don't have one. {_HIDDEN}"
        )
    )
    last_active_on: datetime.date | None = Field(
        description=(
            "The last day the learner did anything in the course. Not available yet, so always "
            "empty for now."
        )
    )

    @model_validator(mode="after")
    def _gate_outcomes(self) -> Self:
        if not self.outcomes_shared:
            for name in _OUTCOME_FIELDS:
                setattr(self, name, None)
        return self


class CompletionStatusCounts(BaseModel):
    """Matches ``LearnerProgressResponse.total_count``'s own filters, not the
    contract as a whole, so it narrows along with the table it summarizes."""

    not_started: int = Field(description="Matching enrollments that haven't been started yet.")
    in_progress: int = Field(description="Matching enrollments with a nonzero grade so far.")
    passed: int = Field(description="Matching enrollments with a currently passing grade.")
    certified: int = Field(description="Matching enrollments with an unrevoked certificate.")


class LearnerProgressResponse(BaseModel):
    """The org envelope (``organization_id``, ``as_of``, ``total_count``,
    ``data``) plus ``outcomes_withheld_count``, so a client can show how many
    rows carry withheld outcomes without paging through all of them."""

    organization_id: str = Field(description="The organization's ID.")
    as_of: UtcDatetime | None = Field(
        description="When the data was last updated. Empty before the first update."
    )
    total_count: int = Field(
        description="Matching enrollments across all pages, not just this one."
    )
    outcomes_withheld_count: int = Field(
        description=(
            "How many of those enrollments have progress hidden because the learner hasn't "
            "agreed to share it."
        )
    )
    completion_status_counts: CompletionStatusCounts = Field(
        description=(
            "How many of those enrollments are in each stage of completion. Enrollments with "
            "hidden progress aren't counted in any stage."
        )
    )
    data: list[LearnerProgress] = Field(description="This page of enrollments.")
