"""Suppress aggregate rows computed over too few distinct learners.

Decision (Verification & QA epic, spec review 2026-07-02): minimum cohort
size of 5 learners. Rows below that threshold are withheld from the API
response entirely, not returned with a low-confidence badge. Applied here at
the response layer (not in the MVs) so the MVs retain full data for
internal/admin use and the suppression logic lives in one reviewable place.
"""

from __future__ import annotations

from typing import Any

from ol_analytics_api.config import settings


def suppress_small_cohorts(
    rows: list[dict[str, Any]], cohort_size_field: str
) -> list[dict[str, Any]]:
    return [row for row in rows if row.get(cohort_size_field, 0) >= settings.anonymization_floor]
