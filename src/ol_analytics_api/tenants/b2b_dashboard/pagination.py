"""Shared LIMIT/OFFSET pagination for this tenant's list endpoints.

Every multi-row endpoint depends on ``pagination`` so an unbounded grain
(content-engagement, enrollment-funnel) can never be pulled whole into the
pod: the query always carries a ``LIMIT`` capped at ``max_page_size``, with
``default_page_size`` applied when the caller passes nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

from ol_analytics_api.tenants.b2b_dashboard.config import settings


class Pagination(BaseModel):
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=settings.max_page_size)] = settings.default_page_size,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)
