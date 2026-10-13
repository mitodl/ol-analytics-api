"""Shared LIMIT/OFFSET pagination for this tenant's list endpoints.

Every multi-row endpoint depends on ``pagination`` so an unbounded grain
(content-engagement, enrollment-funnel) can never be pulled whole into the
pod: the query always carries a ``LIMIT`` capped at ``max_page_size``, with
``default_page_size`` applied when the caller passes nothing.

``learner_pagination`` is the same bound for the endpoint that pages by
learner, where one learner is several rows, so it has its own, lower cap.
"""

# No `from __future__ import annotations`: `_pagination`'s dependency reads
# its bounds from the enclosing call, and FastAPI resolves string annotations
# against module globals, where those bounds don't exist.
from collections.abc import Callable
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

from ol_analytics_api.tenants.b2b_dashboard.config import settings


class Pagination(BaseModel):
    limit: int
    offset: int


def _pagination(default_limit: int, max_limit: int) -> Callable[..., Pagination]:
    def dependency(
        limit: Annotated[int, Query(ge=1, le=max_limit)] = default_limit,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Pagination:
        return Pagination(limit=limit, offset=offset)

    return dependency


pagination = _pagination(settings.default_page_size, settings.max_page_size)
learner_pagination = _pagination(settings.default_learner_page_size, settings.max_learner_page_size)
