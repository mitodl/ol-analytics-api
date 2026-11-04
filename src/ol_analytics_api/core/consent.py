"""Learner consent resolution shared by the tenants that return learner rows.

Both expressions read ``outcomes_decision``, the name a tenant's inner select
gives the MVs' nullable ``outcomes_shared`` (null = no recorded decision,
ol-data-platform#2785). One definition, so the tenants cannot drift on what a
recorded decision means.
"""

from __future__ import annotations

from enum import StrEnum


class ConsentStatus(StrEnum):
    """``not_recorded`` = the learner has not answered. ``declined`` covers a
    learner who answered no and one who consented and later withdrew: MITx
    Online keeps one nullable boolean per (learner, contract)."""

    CONSENTED = "consented"
    DECLINED = "declined"
    NOT_RECORDED = "not_recorded"


# Not consent-gated: it describes the decision, not an outcome, and the
# organization can already see which records are withheld.
CONSENT_STATUS_SQL = (
    "CASE"
    f" WHEN outcomes_decision IS NULL THEN '{ConsentStatus.NOT_RECORDED.value}'"
    f" WHEN outcomes_decision THEN '{ConsentStatus.CONSENTED.value}'"
    f" ELSE '{ConsentStatus.DECLINED.value}'"
    " END"
)


def outcomes_shared_sql(*, fail_open: bool) -> str:
    """Whether a record's outcomes may be disclosed.

    A recorded decision always wins. ``fail_open`` only covers learners with
    none.
    """
    return f"COALESCE(outcomes_decision, {'TRUE' if fail_open else 'FALSE'})"
