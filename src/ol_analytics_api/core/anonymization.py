"""Enforce a k-anonymity floor across every disclosive column of an aggregate row.

A generic k-anonymity-style floor applied at the response layer (not in the
source views, so those retain full data for internal/admin use and the
suppression logic lives in one reviewable place). The floor value itself is a
governance decision each tenant sets in its own config (e.g. the b2b_dashboard
tenant's 5-learner floor, see Verification & QA epic, spec review 2026-07-02),
not a cross-tenant default.

A materialized-view row is not a single cohort: alongside its headline cohort
size it carries many *other* distinct-entity counts (learners who engaged,
earned a certificate, used the chatbot, ...) and values derived from them
(rates, averages, activity sums). Enforcing the floor on only one field lets a
surviving row still disclose the small counts — a row with 50 enrolled but
``certificates_earned == 1`` names that one learner's outcome, and an average
computed over a single engaged learner *is* that learner's exact value. So the
floor is enforced per-column, driven by a `CohortPolicy` the row model declares:

- the ``primary`` cohort gates the whole row (below floor -> row withheld),
- each ``secondary`` count is independently nulled when it is sub-floor, and
- each ``derived`` value is nulled whenever a cohort it is computed over is
  suppressed (else the hidden count is trivially back-computed from the rate,
  or read off directly as an average over k<floor entities).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class CohortPolicy:
    """Which columns of an aggregate row are subject to the k-anonymity floor.

    ``primary``
        The row's headline cohort size. A row whose primary count is below the
        floor (or NULL/missing) is dropped from the response entirely.
    ``secondary``
        Other distinct-entity counts in the same row. Each is independently
        nulled when it is a nonzero sub-floor count (fewer than ``floor``
        identifiable entities). A count of exactly 0 is kept — it discloses no
        individual, only that nobody did the thing.
    ``derived``
        Maps a computed column (a rate, an average, or an activity total that
        is attributable to a cohort) to the cohort count(s) it is derived from.
        The derived value is nulled whenever any of those cohorts is suppressed.
    """

    primary: str
    secondary: tuple[str, ...] = ()
    derived: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A `derived` entry naming a cohort that's neither `primary` nor in
        # `secondary` would silently skip suppression for that derived value
        # — a typo or omission here is a k-anonymity leak, not a cosmetic
        # bug, so it's caught at policy-definition time rather than at
        # response time.
        allowed = {self.primary, *self.secondary}
        for column, cohorts in self.derived.items():
            for cohort in cohorts:
                if cohort not in allowed:
                    msg = (
                        f"Derived column {column!r} references unknown cohort {cohort!r}. "
                        f"It must be either the primary cohort {self.primary!r} or in "
                        "secondary cohorts."
                    )
                    raise ValueError(msg)
        # `frozen=True` stops attribute *reassignment*, not mutation of a
        # mutable object already stored in one — a plain dict handed in (or
        # reused across CohortPolicy instances by a caller) could still be
        # mutated in place afterwards. Copy into a read-only view so it
        # can't be.
        object.__setattr__(self, "derived", MappingProxyType(dict(self.derived)))


def _is_disclosive(value: int | None, floor: int) -> bool:
    # NULL (unknown — e.g. a LEFT JOIN miss in the source MV) is treated as
    # disclosive: we cannot prove the cohort was large enough, so suppress it.
    # Exactly 0 is safe (it names no individual); 1..floor-1 identifies too few.
    if value is None:
        return True
    return 0 < value < floor


def suppress_small_cohorts(
    rows: list[dict[str, Any]], policy: CohortPolicy, floor: int
) -> list[dict[str, Any]]:
    """Apply ``policy`` to every row, returning new dicts with sub-floor cohort
    counts and their derived values nulled, and rows below the primary floor
    dropped. Input rows are not mutated."""
    kept: list[dict[str, Any]] = []
    for row in rows:
        # `.get(field) or 0` folds both a missing key and a NULL primary to 0,
        # so a too-small *or* unknown headline cohort withholds the whole row.
        if (row.get(policy.primary) or 0) < floor:
            continue
        redacted = dict(row)
        suppressed = {
            column for column in policy.secondary if _is_disclosive(redacted.get(column), floor)
        }
        for column in suppressed:
            redacted[column] = None
        for column, cohorts in policy.derived.items():
            if any(cohort in suppressed for cohort in cohorts):
                redacted[column] = None
        kept.append(redacted)
    return kept
