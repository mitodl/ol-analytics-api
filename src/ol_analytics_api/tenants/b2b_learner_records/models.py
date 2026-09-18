"""Response schemas for docs/openapi/b2b-learner-records-v1.yaml.

Plain Pydantic models with no ``cohort_policy``. These records identify
individual learners by design, so nothing in this tenant imports
core/anonymization.py.

Consent gates outcome fields, not the record. Each record model nulls its
outcome fields unless ``outcomes_shared`` is true. The queries already project
NULL for them (see queries._outcomes_shared). This is a second check, so a
query change that projects a raw outcome column still can't disclose it.

The consent date isn't in the warehouse yet, so it is projected as NULL and
ships null until the upstream model lands. It has no default, so the generated
schema lists it as required and nullable, as the contract does.
"""

from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator


def _assume_utc(value: datetime.datetime) -> datetime.datetime:
    # The MVs carry MITx Online timestamps as zone-less ISO-8601 strings in UTC,
    # and StarRocks reports refresh times in its configured zone, which is UTC
    # (ol-infrastructure applications/starrocks). The contract's date-time
    # format needs an offset.
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)


UtcDatetime = Annotated[datetime.datetime, AfterValidator(_assume_utc)]

# record_updated_on carries no activity: a day's activity first appears at a
# refresh after that day began, so a cursor derived from it would already sort
# below the updated_since a partner passes and the change would never be sent.
_ACTIVITY_NOT_SYNCED = "Changes to it do not move updated_since; a full reload picks them up."


def _withhold_outcomes[RecordT: BaseModel](record: RecordT, fields: tuple[str, ...]) -> RecordT:
    if not getattr(record, "outcomes_shared"):  # noqa: B009
        for name in fields:
            setattr(record, name, None)
    return record


class MembershipSource(StrEnum):
    """``roster`` = on the organization's membership roster with no enrollments
    (an assigned, unstarted seat). ``enrollment`` = enrolled under a contract
    but absent from the roster (usually a provisioning lag). ``both`` = the
    expected state."""

    ROSTER = "roster"
    ENROLLMENT = "enrollment"
    BOTH = "both"


class CompletionStatus(StrEnum):
    """``passed`` without ``certified`` is normal — certificates are issued on
    a schedule after grading, and audit-mode enrollments never certify."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    CERTIFIED = "certified"


class LearnerRecordsResponse[RowT: BaseModel](BaseModel):
    organization_id: uuid.UUID = Field(description="The organization's Keycloak organization UUID.")
    as_of: UtcDatetime | None = Field(
        description="Last refresh of the backing data. Null before an organization's first refresh."
    )
    total_count: int = Field(description="Matching records across all pages, not this page.")
    outcomes_withheld_count: int = Field(
        description=(
            "Records in total_count carrying outcomes_shared: false. Always 0 on endpoints "
            "with no consent gate."
        )
    )
    data: list[RowT] = Field(description="The requested page of records.")


class CourseRun(BaseModel):
    """One course run covered by one of the organization's contracts. No personal data,
    so no consent gate."""

    organization_id: uuid.UUID = Field(description="The organization's Keycloak organization UUID.")
    organization_name: str = Field(description="Display name of the organization.")
    contract_id: int = Field(
        description="Numeric identifier of the B2B contract.",
        json_schema_extra={"format": "int64"},
    )
    contract_name: str = Field(description="Name of the B2B contract.")
    contract_is_active: bool = Field(description="Whether the contract is currently active.")
    contract_start_date: datetime.date | None = Field(
        description="Null where the contract records no start date."
    )
    contract_end_date: datetime.date | None = Field(
        description="Null where the contract records no end date."
    )
    seat_limit: int | None = Field(ge=0, description="Null means uncapped, not zero.")
    courserun_id: str = Field(
        description="Readable course-run identifier, e.g. `course-v1:MITxT+14.310x+2T2026`."
    )
    courserun_title: str = Field(description="Mutable display title — key on courserun_id.")
    courserun_start_on: UtcDatetime | None = Field(
        description="Start date/time of the course run, or null if unscheduled."
    )
    courserun_end_on: UtcDatetime | None = Field(description="Null for self-paced runs.")


class Learner(BaseModel):
    """One learner's association with the organization."""

    learner_id: uuid.UUID = Field(
        description=(
            "Stable opaque identifier, consistent across endpoints and stable across email "
            "changes. Use as the join key."
        )
    )
    email: str | None = Field(description="Not a join key; may differ from the enrolling address.")
    full_name: str | None = Field(description="Often null.")
    organization_id: uuid.UUID = Field(description="The organization's Keycloak organization UUID.")
    organization_name: str = Field(description="Display name of the organization.")
    membership_source: MembershipSource = Field(
        description=(
            "How the learner is associated with the organization; see MembershipSource for "
            "the individual values."
        )
    )
    is_organization_manager: bool = Field(
        description="Administers the organization in MITx Online."
    )
    first_enrolled_on: UtcDatetime | None = Field(
        description="Null for a roster member with no enrollments."
    )
    last_enrolled_on: UtcDatetime | None = Field(
        description="Timestamp of the learner's most recent enrollment."
    )
    courses_enrolled: int = Field(
        description="Distinct course runs under the organization's contracts. Not consent-gated."
    )
    outcomes_shared: bool = Field(
        description=(
            "Whether this learner has opted in to sharing their course status. False means "
            "every field below is null."
        )
    )
    outcomes_consent_on: UtcDatetime | None = Field(
        description="When consent was recorded. Null when outcomes_shared is false."
    )
    last_active_on: datetime.date | None = Field(
        description=(
            "Most recent day with tracked course activity (video play, problem check, "
            "navigation, discussion or chatbot submit) across the enrollments the request "
            "covers. A date in the course platform's local day, not a UTC day. Null with no "
            f"activity. {_ACTIVITY_NOT_SYNCED}"
        )
    )
    courses_in_progress: int | None = Field(
        description=(
            "Distinct course runs whose completion_status is in_progress: not passed or "
            "certified, with a nonzero grade or any tracked activity. "
            f"{_ACTIVITY_NOT_SYNCED}"
        )
    )
    courses_passed: int | None = Field(
        description="Distinct course runs where the learner has a passing grade."
    )
    courses_certified: int | None = Field(
        description="Distinct course runs where the learner holds a non-revoked certificate."
    )
    certificates_earned: int | None = Field(
        description=(
            "Includes program certificates, which have no course run and so are not in "
            "courses_certified."
        )
    )

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

    learner_id: uuid.UUID = Field(
        description=(
            "Stable opaque identifier, consistent across endpoints and stable across email "
            "changes. Use as the join key."
        )
    )
    email: str | None = Field(description="Not a join key; may differ from the enrolling address.")
    full_name: str | None = Field(description="Often null.")
    organization_id: uuid.UUID = Field(description="The organization's Keycloak organization UUID.")
    contract_id: int = Field(
        description="Numeric identifier of the B2B contract the enrollment is attributed to.",
        json_schema_extra={"format": "int64"},
    )
    contract_name: str = Field(description="Name of the B2B contract.")
    courserun_id: str = Field(
        description="Readable course-run identifier, e.g. `course-v1:MITxT+14.310x+2T2026`."
    )
    courserun_title: str = Field(description="Mutable display title — key on courserun_id.")
    courserun_start_on: UtcDatetime | None = Field(
        description="Start date/time of the course run, or null if unscheduled."
    )
    courserun_end_on: UtcDatetime | None = Field(description="Null for self-paced runs.")
    enrolled_on: UtcDatetime = Field(
        description=(
            "This run's enrollment, not an earlier run of the same course. Not consent-gated."
        )
    )
    enrollment_is_active: bool = Field(description="Not consent-gated.")
    enrollment_mode: str | None = Field(
        description=("e.g. `verified`, `audit`. Determines whether the run is certificate-bearing.")
    )
    enrollment_status: str | None = Field(description="Deactivation reason where one was recorded.")
    outcomes_shared: bool = Field(
        description=(
            "Whether the learner has opted in to sharing their course status. False means "
            "every field below is null."
        )
    )
    completion_status: CompletionStatus | None = Field(
        description=(
            "Single derived answer per row. Null when outcomes are withheld. in_progress "
            "means not passed or certified, with a nonzero grade or any tracked activity in "
            "the run; not_started means neither."
        )
    )
    is_passing: bool | None = Field(description="Null where no grade has been computed.")
    grade: float | None = Field(description="Numeric grade between 0 and 1.")
    letter_grade: str | None = Field(
        description="Frequently null — not every platform records one."
    )
    certificate_issued_on: UtcDatetime | None = Field(
        description="Timestamp the certificate was issued, or null if none exists."
    )
    certificate_is_revoked: bool | None = Field(
        description=(
            "Null where no certificate exists. A revoked certificate doesn't count as certified: "
            "completion_status then follows the grade, so it can read passed, in_progress or "
            "not_started."
        )
    )
    last_active_on: datetime.date | None = Field(
        description=(
            "Most recent day with tracked activity in this course run. A date in the course "
            f"platform's local day, not a UTC day. Null with no activity. {_ACTIVITY_NOT_SYNCED}"
        )
    )
    days_active: int | None = Field(
        description=(
            f"Distinct days with tracked activity in this course run. {_ACTIVITY_NOT_SYNCED}"
        )
    )
    videos_watched: int | None = Field(
        description=(
            "Video blocks played, counted once per day: a block played on two days counts "
            f"twice. {_ACTIVITY_NOT_SYNCED}"
        )
    )
    problems_attempted: int | None = Field(
        description=(
            "Problem blocks checked, counted once per day: a block attempted on two days "
            f"counts twice. Viewing an answer is not an attempt. {_ACTIVITY_NOT_SYNCED}"
        )
    )
    chatbot_interactions: int | None = Field(
        description=(
            "Chatbot submits in this course run, counting each (session, block) once per day. "
            f"{_ACTIVITY_NOT_SYNCED}"
        )
    )

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
