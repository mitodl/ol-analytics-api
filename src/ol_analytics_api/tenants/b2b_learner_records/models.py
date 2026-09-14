"""Response schemas for docs/openapi/b2b-learner-records-v1.yaml.

Plain Pydantic models with no ``cohort_policy``. These records identify
individual learners by design, so nothing in this tenant imports
core/anonymization.py.

Consent gates outcome fields, not the record. Each record model nulls its
outcome fields unless ``outcomes_shared`` is true. The queries already project
NULL for them (see queries._OUTCOMES_SHARED). This is a second check, so a
query change that projects a raw outcome column still can't disclose it.

Fields the warehouse doesn't carry yet (consent date, activity) are projected
as NULL and ship null until upstream models land. They have no default, so
the generated schema lists them as required and nullable, as the contract does.
"""

from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, model_validator


def _assume_utc(value: datetime.datetime) -> datetime.datetime:
    # The MVs carry MITx Online timestamps as zone-less ISO-8601 strings in UTC,
    # and StarRocks reports refresh times in its configured zone, which is UTC
    # (ol-infrastructure applications/starrocks). The contract's date-time
    # format needs an offset.
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)


UtcDatetime = Annotated[datetime.datetime, AfterValidator(_assume_utc)]


def _withhold_outcomes[RecordT: BaseModel](record: RecordT, fields: tuple[str, ...]) -> RecordT:
    if not getattr(record, "outcomes_shared"):  # noqa: B009
        for name in fields:
            setattr(record, name, None)
    return record


class MembershipSource(StrEnum):
    ROSTER = "roster"
    ENROLLMENT = "enrollment"
    BOTH = "both"


class CompletionStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    CERTIFIED = "certified"


class LearnerRecordsResponse[RowT: BaseModel](BaseModel):
    organization_id: uuid.UUID
    # Last refresh of the backing view(s). Null before the first refresh.
    as_of: UtcDatetime | None
    total_count: int
    outcomes_withheld_count: int
    data: list[RowT]


class Learner(BaseModel):
    """One learner's association with the organization."""

    learner_id: uuid.UUID
    email: str | None
    full_name: str | None
    organization_id: uuid.UUID
    organization_name: str
    membership_source: MembershipSource
    is_organization_manager: bool
    first_enrolled_on: UtcDatetime | None
    last_enrolled_on: UtcDatetime | None
    courses_enrolled: int
    outcomes_shared: bool
    outcomes_consent_on: UtcDatetime | None
    last_active_on: datetime.date | None
    courses_in_progress: int | None
    courses_passed: int | None
    courses_certified: int | None
    certificates_earned: int | None

    @model_validator(mode="after")
    def _gate_outcomes(self) -> Self:
        return _withhold_outcomes(
            self,
            (
                "outcomes_consent_on",
                "last_active_on",
                "courses_in_progress",
                "courses_passed",
                "courses_certified",
                "certificates_earned",
            ),
        )


class Enrollment(BaseModel):
    """One learner's enrollment in one course run under one contract."""

    learner_id: uuid.UUID
    email: str | None
    full_name: str | None
    organization_id: uuid.UUID
    contract_id: int
    contract_name: str
    courserun_id: str
    courserun_title: str
    courserun_start_on: UtcDatetime | None
    courserun_end_on: UtcDatetime | None
    enrolled_on: UtcDatetime
    enrollment_is_active: bool
    enrollment_mode: str | None
    enrollment_status: str | None
    outcomes_shared: bool
    completion_status: CompletionStatus | None
    is_passing: bool | None
    grade: float | None
    letter_grade: str | None
    certificate_issued_on: UtcDatetime | None
    certificate_is_revoked: bool | None
    last_active_on: datetime.date | None
    days_active: int | None
    videos_watched: int | None
    problems_attempted: int | None
    chatbot_interactions: int | None

    @model_validator(mode="after")
    def _gate_outcomes(self) -> Self:
        return _withhold_outcomes(
            self,
            (
                "completion_status",
                "is_passing",
                "grade",
                "letter_grade",
                "certificate_issued_on",
                "certificate_is_revoked",
                "last_active_on",
                "days_active",
                "videos_watched",
                "problems_attempted",
                "chatbot_interactions",
            ),
        )
