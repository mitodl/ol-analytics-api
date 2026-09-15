"""Response schemas for the learner-progress endpoint.

Kept apart from models.py, where every row model declares a ``cohort_policy``.
These rows identify individual learners, so no floor applies and nothing here
imports core/anonymization.py.

Consent gates outcome fields, not the row. The query already projects NULL for
withheld outcomes (learner_queries._outcomes_shared); the validator below is a
second check, so a query change that projects a raw outcome column still can't
disclose it.
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
        description=(
            "Keycloak user id (MITx Online's user global_id). Stable across email changes; "
            "use it to join against MITx Online's manager enrollment list."
        )
    )
    email: str | None = Field(description="Not a join key; may differ from the enrolling address.")
    full_name: str | None = Field(description="Often null.")
    courserun_readable_id: str = Field(
        description="Readable course-run identifier, e.g. `course-v1:MITxT+14.310x+2T2026`."
    )
    courserun_title: str = Field(description="Mutable display title; key on courserun_readable_id.")
    courserun_start_on: UtcDatetime | None
    courserun_end_on: UtcDatetime | None = Field(description="Null for self-paced runs.")
    enrolled_on: UtcDatetime = Field(description="When this run's enrollment was created.")
    enrollment_is_active: bool = Field(description="Not consent-gated.")
    enrollment_mode: str | None = Field(description="e.g. `verified`, `audit`.")
    outcomes_shared: bool = Field(
        description="Whether the learner shares their course status. False means every field "
        "below is null."
    )
    completion_status: CompletionStatus | None = Field(
        description=(
            "Until activity data lands, not_started and in_progress come from the grade alone: "
            "in_progress means a nonzero grade."
        )
    )
    is_passing: bool | None = Field(description="Null where no grade has been computed.")
    grade: float | None = Field(description="Numeric grade between 0 and 1.")
    letter_grade: str | None = Field(description="Frequently null.")
    certificate_issued_on: UtcDatetime | None
    certificate_is_revoked: bool | None = Field(
        description="Null where no certificate exists. A revoked certificate doesn't count as "
        "certified: completion_status then follows the grade, so it can read passed, "
        "in_progress or not_started."
    )
    last_active_on: datetime.date | None = Field(
        description=(
            "Most recent day with course activity in this run. Null for every row until "
            "learner-grain activity data lands, whatever outcomes_shared says."
        )
    )

    @model_validator(mode="after")
    def _gate_outcomes(self) -> Self:
        if not self.outcomes_shared:
            for name in _OUTCOME_FIELDS:
                setattr(self, name, None)
        return self


class LearnerProgressResponse(BaseModel):
    """The org envelope (``organization_id``, ``as_of``, ``total_count``,
    ``data``) plus ``outcomes_withheld_count``, so a client can show how many
    rows carry withheld outcomes without paging through all of them."""

    organization_id: str
    as_of: UtcDatetime | None
    total_count: int = Field(description="Matching rows across all pages, not this page.")
    outcomes_withheld_count: int = Field(
        description="Rows in total_count carrying outcomes_shared: false."
    )
    data: list[LearnerProgress]
