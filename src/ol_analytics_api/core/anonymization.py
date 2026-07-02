"""Suppress aggregate rows computed over too few distinct entities.

A generic k-anonymity-style floor: rows below `floor` on `cohort_size_field`
are withheld from the API response entirely, not returned with a
low-confidence badge. Applied at the response layer (not in the source
views) so those retain full data for internal/admin use and the suppression
logic lives in one reviewable place per tenant — the floor value itself is a
governance decision each tenant sets in its own config (e.g. the
b2b_dashboard tenant's 5-learner floor, see Verification & QA epic,
spec review 2026-07-02), not a cross-tenant default.
"""

from __future__ import annotations

from typing import Any


def suppress_small_cohorts(
    rows: list[dict[str, Any]], cohort_size_field: str, floor: int
) -> list[dict[str, Any]]:
    return [row for row in rows if row.get(cohort_size_field, 0) >= floor]
